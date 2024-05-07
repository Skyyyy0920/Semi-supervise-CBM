import numpy as np
import os
import pytorch_lightning as pl
import time
import torch
import logging

from pytorch_lightning import seed_everything
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.loggers import WandbLogger
from scipy.special import expit
from sklearn.metrics import accuracy_score
from tqdm import tqdm
import tensorflow as tf

import cem.metrics.niching as niching
import cem.metrics.oracle as oracle
import cem.train.utils as utils

from cem.metrics.cas import concept_alignment_score
from cem.models.construction import (
    construct_model,
    construct_sequential_models,
    load_trained_model,
)


def train_end_to_end_model(
        n_concepts,
        n_tasks,
        config,
        train_dl,
        val_dl,
        run_name,
        result_dir=None,
        test_dl=None,
        imbalance=None,
        task_class_weights=None,
        rerun=False,
        logger=False,
        project_name='',
        seed=42,
        save_model=True,
        activation_freq=0,
        single_frequency_epochs=0,
        gradient_clip_val=0,
        old_results=None,
        enable_checkpointing=False,
        accelerator="auto",
        devices="auto",
):
    seed_everything(seed)

    full_run_name = "test"

    # create model
    model = construct_model(
        n_concepts,
        n_tasks,
        config,
        imbalance=imbalance,
        task_class_weights=task_class_weights,
    )
    logging.info(f"Number of parameters in model: {sum(p.numel() for p in model.parameters() if p.requires_grad)}")
    logging.info(f"[Number of non-trainable parameters in model: "
                 f"{sum(p.numel() for p in model.parameters() if not p.requires_grad)}")

    if config.get("model_pretrain_path"):
        if os.path.exists(config.get("model_pretrain_path")):
            logging.info("Load pretrained model")
            model.load_state_dict(torch.load(config.get("model_pretrain_path")), strict=False)

    callbacks = [
        EarlyStopping(
            monitor=config["early_stopping_monitor"],
            min_delta=config.get("early_stopping_delta", 0.00),
            patience=config['patience'],
            verbose=config.get("verbose", False),
            mode=config["early_stopping_mode"],
        ),
    ]

    trainer = pl.Trainer(
        accelerator=accelerator,
        devices=devices,
        max_epochs=config['max_epochs'],
        check_val_every_n_epoch=config.get("check_val_every_n_epoch", 5),
        callbacks=callbacks,
        logger=logger or False,
        enable_checkpointing=enable_checkpointing,
        gradient_clip_val=gradient_clip_val,
    )

    if result_dir:
        if activation_freq:
            fit_trainer = utils.ActivationMonitorWrapper(
                model=model,
                trainer=trainer,
                activation_freq=activation_freq,
                single_frequency_epochs=single_frequency_epochs,
                output_dir=os.path.join(
                    result_dir,
                    f"test_embedding_acts/{full_run_name}",
                ),
                # YES, we pass the validation data intentionally to avoid
                # explosion of memory usage
                test_dl=val_dl,
            )
        else:
            fit_trainer = trainer
    else:
        fit_trainer = trainer

    # Else it is time to train it
    model_saved_path = os.path.join(
        result_dir or ".",
        f'{full_run_name}.pt'
    )
    if (not rerun) and os.path.exists(model_saved_path):
        # Then we simply load the model and proceed
        print("\tFound cached model... loading it")
        model.load_state_dict(torch.load(model_saved_path))
        if os.path.exists(
                model_saved_path.replace(".pt", "_training_times.npy")
        ):
            [training_time, num_epochs] = np.load(
                model_saved_path.replace(".pt", "_training_times.npy"),
            )
        else:
            training_time, num_epochs = 0, 0
    else:
        # Else it is time to train it
        start_time = time.time()
        fit_trainer.fit(model, train_dl, val_dl)
        if fit_trainer.interrupted:
            reply = None
            while reply not in ['y', 'n']:
                if reply is not None:
                    print("Please provide only either 'y' or 'n'.")
                reply = input(
                    "Would you like to manually interrupt this model's "
                    "training and continue the experiment? [y/n]\n"
                ).strip().lower()
            if reply == "n":
                raise ValueError(
                    'Experiment execution was manually interrupted!'
                )
        training_time = time.time() - start_time
        num_epochs = fit_trainer.current_epoch
        if save_model and (result_dir is not None):
            torch.save(
                model.state_dict(),
                model_saved_path,
            )
            np.save(
                model_saved_path.replace(".pt", "_training_times.npy"),
                np.array([training_time, num_epochs]),
            )

    if not os.path.exists(os.path.join(
            result_dir,
            f'{run_name}_experiment_config.joblib'
    )):
        # Then let's serialize the experiment config for this run
        config_copy = copy.deepcopy(config)
        if "c_extractor_arch" in config_copy and (
                not isinstance(config_copy["c_extractor_arch"], str)
        ):
            del config_copy["c_extractor_arch"]
        joblib.dump(config_copy, os.path.join(
            result_dir,
            f'{run_name}_experiment_config.joblib'
        ))
    eval_results = _evaluate_cbm(
        model=model,
        trainer=trainer,
        config=config,
        run_name=run_name,
        old_results=old_results,
        rerun=rerun,
        test_dl=test_dl,
        val_dl=val_dl,
    )
    eval_results['training_time'] = training_time
    eval_results['num_epochs'] = num_epochs
    if test_dl is not None:
        print(
            f'c_acc: {eval_results["test_acc_c"] * 100:.2f}%, '
            f'y_acc: {eval_results["test_acc_y"] * 100:.2f}%, '
            f'c_auc: {eval_results["test_auc_c"] * 100:.2f}%, '
            f'y_auc: {eval_results["test_auc_y"] * 100:.2f}% with '
            f'{num_epochs} epochs in {training_time:.2f} seconds'
        )

    return model, eval_results