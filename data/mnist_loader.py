import logging
import numpy as np
import os
import random
import sklearn.model_selection
import torch
import torchvision
from pytorch_lightning import seed_everything
from torch.utils.data import Dataset, TensorDataset, DataLoader, random_split


def get_ss_components(xs, ys, cs, labeled_ratio, training=False, seed=42):
    from collections import defaultdict
    from sklearn.neighbors import NearestNeighbors

    def nearest_neighbors(xs, l_choice, k=3):
        size = xs.shape[0]
        features = xs.copy().reshape(size, -1)
        labeled_features = []
        for idx in range(size):
            if l_choice[idx]:
                labeled_features.append(features[idx])
        labeled_features = np.array(labeled_features)
        nbrs = NearestNeighbors(n_neighbors=k, metric='cosine')
        nbrs.fit(labeled_features)
        distances, indices = nbrs.kneighbors(features)

        weights = 1.0 / (distances + 1e-6)
        weights = weights / np.sum(weights, axis=1, keepdims=True)

        return [{'indices': idx, 'weights': w} for idx, w in zip(indices, weights)]

    l_choice = defaultdict(bool)
    if training:
        random.seed(seed)
        class_count = defaultdict(int)
        for y in ys:
            class_count[y] += 1
        labeled_count = defaultdict(int)
        for idx, y in enumerate(ys):
            class_label = y
            if labeled_count[class_label] < labeled_ratio * class_count[class_label]:
                l_choice[idx] = True
                labeled_count[class_label] += 1
            else:
                l_choice[idx] = False
    else:
        for idx in range(len(ys)):
            l_choice[idx] = True

    count = 0
    for idx in range(len(l_choice)):
        if l_choice[idx]:
            count += 1
    logging.info(f"actual labeled ratio: {count / len(l_choice)}")

    nbrs = nearest_neighbors(xs, l_choice, k=2)

    l_list, nbr_concepts_list, nbr_weights_list = [], [], []
    for i in range(len(ys)):
        l = l_choice[idx]
        nbr_info = nbrs[idx]
        nbr_indices = nbr_info['indices']
        nbr_concepts = []
        for i in nbr_indices:
            nbr_concepts.append(cs[i])
        nbr_concepts = np.array(nbr_concepts)
        nbr_weights = nbr_info['weights']

        l_list.append(l)
        nbr_concepts_list.append(nbr_concepts)
        nbr_weights_list.append(nbr_weights)

    l, nbr_concepts, nbr_weights = np.array(l_list), np.array(nbr_concepts_list), np.array(nbr_weights_list)
    return torch.tensor(xs), torch.tensor(ys).squeeze(), torch.tensor(cs), \
           torch.tensor(l), torch.tensor(nbr_concepts), torch.tensor(nbr_weights)


def inject_uncertainty(
        *datasets,
        uncertain_width=0,
        concept_groups=None,
        batch_size=512,
        num_workers=-1,
        mixing=True,
        threshold=False,
):
    seed_everything(42)
    results = []
    concept_groups = concept_groups or []
    for ds in datasets:
        xs = []
        ys = []
        cs = []

        for x, y, c in ds:
            ys.append(y)
            c_new = c.numpy()
            x_new = x.numpy()
            if uncertain_width:
                for j in range(c_new.shape[-1]):
                    num_operands = x.shape[1] if x.shape[1] > 2 else 1
                    if mixing:
                        possible_options_pos = x[
                                               c[:, j] == 1,
                                               j // num_operands,
                                               :,
                                               :,
                                               ]
                        possible_options_neg = x[
                                               c[:, j] == 0,
                                               j // num_operands,
                                               :,
                                               :,
                                               ]
                    for i in range(c_new.shape[0]):
                        if c_new[i, j] == 1:
                            c_new[i, j] = np.random.uniform(
                                low=1.0 - uncertain_width,
                                high=1,
                            )
                            if mixing:
                                selected_mix = np.random.randint(
                                    0,
                                    possible_options_neg.shape[0],
                                )
                                x_new[i, j // num_operands, :, :] = (
                                        x_new[i, j // num_operands, :, :] * c_new[i, j] +
                                        (
                                                (1 - c_new[i, j]) *
                                                possible_options_neg[
                                                selected_mix,
                                                :,
                                                :,
                                                ].numpy()
                                        )
                                )
                        else:
                            c_new[i, j] = np.random.uniform(
                                low=0.0,
                                high=uncertain_width,
                            )
                            if mixing:
                                selected_mix = np.random.randint(
                                    0,
                                    possible_options_pos.shape[0],
                                )
                                x_new[i, j // num_operands, :, :] = (
                                        x_new[i, j // num_operands, :, :] * (1 - c_new[i, j]) +
                                        c_new[i, j] * possible_options_pos[selected_mix, :, :].numpy()
                                )
                        if threshold:
                            c_new[i, j] = int(c_new[i, j] >= 0.5)

            xs.append(x_new)
            cs.append(c_new)
        x = torch.FloatTensor(np.concatenate(xs, axis=0))

        y = torch.cat(ys, dim=0)
        c = torch.FloatTensor(np.concatenate(cs, axis=0))
        results.append(DataLoader(TensorDataset(x, y, c), batch_size=batch_size, num_workers=num_workers))
    return results


def produce_addition_set(
        X,
        y,
        dataset_size=30000,
        num_operands=2,
        selected_digits=list(range(10)),
        output_channels=1,
        img_format='channels_first',
        sample_concepts=None,
        normalize_samples=True,
        concat_dim='channels',
        even_concepts=False,
        even_labels=False,
        threshold_labels=None,
        concept_transform=None,
        noise_level=0.0,
):
    filter_idxs = []
    if len(y.shape) == 2 and y.shape[-1] == 1:
        y = y[:, 0]
    if not isinstance(selected_digits[0], list):
        selected_digits = [selected_digits[:] for _ in range(num_operands)]
    elif len(selected_digits) != num_operands:
        raise ValueError(
            "If selected_digits is a list of lists, it must have the same "
            "length as num_operands"
        )

    operand_remaps = [
        dict((dig, idx) for (idx, dig) in enumerate(operand_digits))
        for operand_digits in selected_digits
    ]
    total_operand_samples = []
    total_operand_labels = []
    for allowed_digits in selected_digits:
        filter_idxs = []
        for idx, digit in enumerate(y):
            if digit in allowed_digits:
                filter_idxs.append(idx)
        total_operand_samples.append(X[filter_idxs, :, :, :])
        total_operand_labels.append(y[filter_idxs])

    sum_train_samples = []
    sum_train_concepts = []
    sum_train_labels = []
    for i in range(dataset_size):
        operands = []
        concepts = []
        sample_label = 0
        selected = []
        for operand_digits, remap, total_samples, total_labels in zip(
                selected_digits,
                operand_remaps,
                total_operand_samples,
                total_operand_labels,
        ):
            img_idx = np.random.choice(total_samples.shape[0])
            selected.append(total_labels[img_idx])
            img = total_samples[img_idx: img_idx + 1, :, :, :].copy()
            if len(operand_digits) > 2:
                if even_concepts:
                    concept_vals = np.array([[
                        int((remap[total_labels[img_idx]] % 2) == 0)
                    ]])
                else:
                    concept_vals = torch.nn.functional.one_hot(
                        torch.LongTensor([remap[total_labels[img_idx]]]),
                        num_classes=len(operand_digits)
                    ).numpy()
                concepts.append(concept_vals)
            else:
                # Else we will treat it as a simple binary concept (this allows
                # us to train models that do not have mutually exclusive
                # concepts!)
                if even_concepts:
                    concepts.append(np.array([[
                        int((total_labels[img_idx] % 2) == 0)
                    ]]))
                else:
                    max_bound = np.max(operand_digits)
                    val = int(total_labels[img_idx] == max_bound)
                    concepts.append(np.array([[val]]))
            sample_label += total_labels[img_idx]
            operands.append(img)
        if concat_dim == 'channels':
            sum_train_samples.append(np.concatenate(operands, axis=3))
        elif concat_dim == 'x':
            sum_train_samples.append(np.concatenate(operands, axis=2))
        else:
            sum_train_samples.append(np.concatenate(operands, axis=1))
        if even_labels:
            sum_train_labels.append(int(sample_label % 2 == 0))
        elif threshold_labels is not None:
            sum_train_labels.append(int(sample_label >= threshold_labels))
        else:
            sum_train_labels.append(sample_label)
        sum_train_concepts.append(np.concatenate(concepts, axis=-1))
    sum_train_samples = np.concatenate(sum_train_samples, axis=0)
    sum_train_concepts = np.concatenate(sum_train_concepts, axis=0)
    sum_train_labels = np.array(sum_train_labels)
    if output_channels != 1 and concat_dim != 'channels':
        sum_train_samples = np.stack(
            (sum_train_samples[:, :, :, 0].astype(np.float32),) * output_channels,
            axis=-1,
        )
    if img_format == 'channels_first':
        sum_train_samples = np.transpose(sum_train_samples, axes=[0, 3, 2, 1])
    if normalize_samples:
        sum_train_samples = sum_train_samples / 255.0
    if sample_concepts is not None:
        sum_train_concepts = sum_train_concepts[:, sample_concepts]
    if concept_transform is not None:
        sum_train_concepts = concept_transform(sum_train_concepts)
    if noise_level > 0.0:
        sum_train_samples = sum_train_samples + np.random.normal(
            loc=0.0,
            scale=noise_level,
            size=sum_train_samples.shape,
        )
        if normalize_samples:
            sum_train_samples = np.clip(
                sum_train_samples,
                a_min=0.0,
                a_max=1.0,
            )
    return sum_train_samples, sum_train_labels, sum_train_concepts


def load_mnist_addition(
        cache_dir="data",
        labeled_ratio=0.2,
        seed=42,
        train_dataset_size=30000,
        test_dataset_size=10000,
        num_operands=10,
        selected_digits=list(range(10)),
        uncertain_width=0,
        renormalize=True,
        val_percent=0.2,
        batch_size=512,
        test_only=False,
        num_workers=-1,
        sample_concepts=None,
        as_channels=True,
        img_format='channels_first',
        output_channels=1,
        threshold=False,
        mixing=True,
        even_concepts=False,
        even_labels=False,
        threshold_labels=None,
        concept_transform=None,
        noise_level=0.0,
        test_noise_level=None,
):
    test_noise_level = (
        test_noise_level if (test_noise_level is not None) else noise_level
    )
    os.environ['PYTHONHASHSEED'] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    # pytorch_lightning.utilities.seed.seed_everything(seed)

    concept_groups = []
    for operand_digits in selected_digits:
        concept_groups.append(list(range(
            len(concept_groups),
            len(concept_groups) + len(operand_digits),
        )))

    ds_test = torchvision.datasets.MNIST(
        cache_dir,
        train=False,
        download=True,
    )

    # Put all the images into a single np array for easy
    # manipulation
    x_test = []
    y_test = []

    for x, y in ds_test:
        x_test.append(np.expand_dims(
            np.expand_dims(x, axis=0),
            axis=-1,
        ))
        y_test.append(np.expand_dims(
            np.expand_dims(y, axis=0),
            axis=-1,
        ))
    x_test = np.concatenate(x_test, axis=0)
    y_test = np.concatenate(y_test, axis=0)

    # Wrap them up in dataloaders
    x_test, y_test, c_test = produce_addition_set(
        X=x_test,
        y=y_test,
        dataset_size=test_dataset_size,
        num_operands=num_operands,
        selected_digits=selected_digits,
        sample_concepts=sample_concepts,
        img_format=img_format,
        output_channels=1 if as_channels else output_channels,
        concat_dim='channels' if as_channels else 'y',
        even_concepts=even_concepts,
        even_labels=even_labels,
        threshold_labels=threshold_labels,
        concept_transform=concept_transform,
        noise_level=test_noise_level,
    )
    x_test = torch.FloatTensor(x_test)
    if even_labels or (threshold_labels is not None):
        y_test = torch.FloatTensor(y_test)
    else:
        y_test = torch.LongTensor(y_test)
    c_test = torch.FloatTensor(c_test)
    x_test, y_test, c_test, l_test, ncs_test, nws_test = get_ss_components(
        x_test.numpy(), y_test.numpy(), c_test.numpy(), labeled_ratio=1.
    )
    test_data = torch.utils.data.TensorDataset(x_test, y_test, c_test, l_test, ncs_test, nws_test)
    test_dl = DataLoader(test_data, batch_size=batch_size, num_workers=num_workers)
    if uncertain_width and (not even_concepts):
        [test_dl] = inject_uncertainty(
            test_dl,
            uncertain_width=uncertain_width,
            concept_groups=concept_groups,
            batch_size=batch_size,
            num_workers=num_workers,
            mixing=mixing,
            threshold=threshold,
        )
    if test_only:
        return test_dl

    # Now time to do the same for the train/val datasets!
    ds_train = torchvision.datasets.MNIST(
        cache_dir,
        train=True,
        download=True,
    )

    x_train = []
    y_train = []

    for x, y in ds_train:
        x_train.append(np.expand_dims(
            np.expand_dims(x, axis=0),
            axis=-1,
        ))
        y_train.append(np.expand_dims(
            np.expand_dims(y, axis=0),
            axis=-1,
        ))

    x_train = np.concatenate(x_train, axis=0)
    y_train = np.concatenate(y_train, axis=0)

    if val_percent:
        x_train, x_val, y_train, y_val = \
            sklearn.model_selection.train_test_split(
                x_train,
                y_train,
                test_size=val_percent,
            )
        x_val, y_val, c_val = produce_addition_set(
            X=x_val,
            y=y_val,
            dataset_size=int(train_dataset_size * val_percent),
            num_operands=num_operands,
            selected_digits=selected_digits,
            sample_concepts=sample_concepts,
            img_format=img_format,
            output_channels=1 if as_channels else output_channels,
            concat_dim='channels' if as_channels else 'y',
            even_concepts=even_concepts,
            even_labels=even_labels,
            threshold_labels=threshold_labels,
            concept_transform=concept_transform,
            noise_level=noise_level,
        )
        x_val = torch.FloatTensor(x_val)
        if even_labels or (threshold_labels is not None):
            y_val = torch.FloatTensor(y_val)
        else:
            y_val = torch.LongTensor(y_val)
        c_val = torch.FloatTensor(c_val)
        x_val, y_val, c_val, l_val, ncs_val, nws_val = get_ss_components(
            x_val.numpy(), y_val.numpy(), c_val.numpy(), labeled_ratio=1.)
        val_data = torch.utils.data.TensorDataset(x_val, y_val, c_val, l_val, ncs_val, nws_val)
        val_dl = DataLoader(val_data, batch_size=batch_size, num_workers=num_workers)
        if uncertain_width and (not even_concepts):
            [val_dl] = inject_uncertainty(
                val_dl,
                uncertain_width=uncertain_width,
                concept_groups=concept_groups,
                batch_size=batch_size,
                num_workers=num_workers,
                mixing=mixing,
                threshold=threshold,
            )
    else:
        val_dl = None

    x_train, y_train, c_train = produce_addition_set(
        X=x_train,
        y=y_train,
        dataset_size=train_dataset_size,
        num_operands=num_operands,
        selected_digits=selected_digits,
        sample_concepts=sample_concepts,
        img_format=img_format,
        output_channels=1 if as_channels else output_channels,
        concat_dim='channels' if as_channels else 'y',
        even_concepts=even_concepts,
        even_labels=even_labels,
        threshold_labels=threshold_labels,
        concept_transform=concept_transform,
        noise_level=noise_level,
    )
    x_train = torch.FloatTensor(x_train)
    if even_labels or (threshold_labels is not None):
        y_train = torch.FloatTensor(y_train)
    else:
        y_train = torch.LongTensor(y_train)
    c_train = torch.FloatTensor(c_train)
    x_train, y_train, c_train, l_train, ncs_train, nws_train = get_ss_components(
        x_train.numpy(), y_train.numpy(), c_train.numpy(), labeled_ratio=labeled_ratio, training=True)
    train_data = torch.utils.data.TensorDataset(x_train, y_train, c_train, l_train, ncs_train, nws_train)
    train_dl = DataLoader(train_data, batch_size=batch_size, num_workers=num_workers)

    if uncertain_width and (not even_concepts):
        [train_dl] = inject_uncertainty(
            train_dl,
            uncertain_width=uncertain_width,
            concept_groups=concept_groups,
            batch_size=batch_size,
            num_workers=num_workers,
            mixing=mixing,
            threshold=threshold,
        )

    return train_dl, val_dl, test_dl


def generate_data(
        config,
        root_dir="data",
        labeled_ratio=0.1,
        seed=42,
        rerun=False
):
    selected_digits = config.get("selected_digits", list(range(2)))
    num_operands = config.get("num_operands", 32)
    if not isinstance(selected_digits[0], list):
        selected_digits = [selected_digits[:] for _ in range(num_operands)]
    elif len(selected_digits) != num_operands:
        raise ValueError("If selected_digits is a list of lists, it must have the same length as num_operands")
    even_concepts = config.get("even_concepts", False)
    even_labels = config.get("even_labels", False)
    threshold_labels = config.get("threshold_labels", None)

    if even_concepts:
        num_concepts = num_operands
        concept_group_map = {i: [i] for i in range(num_operands)}
    else:
        num_concepts = 0
        concept_group_map = {}
        n_tasks = 1  # Zero is always included as a possible sum
        for operand_idx, used_operand_digits in enumerate(selected_digits):
            num_curr_concepts = len(used_operand_digits) if len(used_operand_digits) > 2 else 1
            concept_group_map[operand_idx] = list(range(num_concepts, num_concepts + num_curr_concepts))
            num_concepts += num_curr_concepts
            n_tasks += np.max(used_operand_digits)

    if even_labels or (threshold_labels is not None):
        n_tasks = 1

    sampling_percent = config.get("sampling_percent", 1)
    sampling_groups = config.get("sampling_groups", False)

    if sampling_percent != 1:
        # Do the subsampling
        print("DO the subsampling")
        if sampling_groups:
            new_n_groups = int(np.ceil(len(concept_group_map) * sampling_percent))
            selected_groups_file = os.path.join(
                root_dir,
                f"selected_groups_sampling_{sampling_percent}_operands_{num_operands}.npy",
            )
            if (not rerun) and os.path.exists(selected_groups_file):
                selected_groups = np.load(selected_groups_file)
            else:
                selected_groups = sorted(
                    np.random.permutation(len(concept_group_map))[:new_n_groups]
                )
                np.save(selected_groups_file, selected_groups)
            selected_concepts = []
            group_concepts = [x[1] for x in concept_group_map.items()]
            for group_idx in selected_groups:
                selected_concepts.extend(group_concepts[group_idx])
            selected_concepts = sorted(set(selected_concepts))
            new_n_concepts = len(selected_concepts)
        else:
            new_n_concepts = int(np.ceil(num_concepts * sampling_percent))
            selected_concepts_file = os.path.join(
                root_dir,
                f"selected_concepts_sampling_{sampling_percent}_operands_{num_operands}.npy",
            )
            if (not rerun) and os.path.exists(selected_concepts_file):
                selected_concepts = np.load(selected_concepts_file)
            else:
                selected_concepts = sorted(
                    np.random.permutation(num_concepts)[:new_n_concepts]
                )
                np.save(selected_concepts_file, selected_concepts)
        # Then we also have to update the concept group map so that selected concepts that were previously
        # in the same concept group are maintained in the same concept group
        new_concept_group = {}
        remap = dict((y, x) for (x, y) in enumerate(selected_concepts))
        selected_concepts_set = set(selected_concepts)
        for selected_concept in selected_concepts:
            for concept_group_name, group_concepts in concept_group_map.items():
                if selected_concept in group_concepts:
                    if concept_group_name in new_concept_group:
                        # Then we have already added this group
                        continue
                    # Then time to add this group!
                    new_concept_group[concept_group_name] = []
                    for other_concept in group_concepts:
                        if other_concept in selected_concepts_set:
                            # Add the remapped version of this concept into the concept group
                            new_concept_group[concept_group_name].append(remap[other_concept])

        def concept_transform(sample):
            return sample[:, selected_concepts]

        num_concepts = new_n_concepts
        concept_group_map = new_concept_group
        logging.debug("\t\tSelected concepts:", selected_concepts)
        logging.debug(
            f"\t\tUpdated concept group map "
            f"(with {len(concept_group_map)} groups):"
        )
        for k, v in concept_group_map.items():
            logging.debug(f"\t\t\t{k} -> {v}")
    else:
        concept_transform = None

    train_dl, val_dl, test_dl = load_mnist_addition(
        cache_dir=root_dir,
        labeled_ratio=labeled_ratio,
        seed=seed,
        train_dataset_size=config.get("train_dataset_size", 30000),
        test_dataset_size=config.get("test_dataset_size", 10000),
        num_operands=num_operands,
        selected_digits=selected_digits,
        uncertain_width=config.get("uncertain_width", 0),
        renormalize=config.get("renormalize", True),
        val_percent=config.get("val_percent", 0.2),
        batch_size=config.get("batch_size", 512),
        test_only=config.get("test_only", False),
        num_workers=config.get("num_workers", -1),
        sample_concepts=config.get("sample_concepts", None),
        as_channels=config.get("as_channels", True),
        img_format=config.get("img_format", 'channels_first'),
        output_channels=config.get("output_channels", 1),
        threshold=config.get("threshold", True),
        mixing=config.get("mixing", True),
        even_labels=even_labels,
        threshold_labels=threshold_labels,
        even_concepts=even_concepts,
        concept_transform=concept_transform,
        noise_level=config.get("noise_level", 0),
        test_noise_level=config.get("test_noise_level", config.get("noise_level", 0))
    )

    if config.get('weight_loss', False):
        attribute_count = np.zeros((num_concepts,))
        samples_seen = 0
        for i, data in enumerate(train_dl):
            _, y, c, _, _, _ = data
            c = c.cpu().detach().numpy()
            attribute_count += np.sum(c, axis=0)
            samples_seen += c.shape[0]
        imbalance = samples_seen / attribute_count - 1
    else:
        imbalance = None

    return train_dl, val_dl, test_dl, imbalance, (num_concepts, n_tasks, concept_group_map)


def get_mnist_extractor_arch(input_shape, num_operands):
    def c_extractor_arch(output_dim):
        intermediate_maps = 16
        output_dim = output_dim or 128
        return torch.nn.Sequential(*[
            torch.nn.Conv2d(
                in_channels=num_operands,
                out_channels=intermediate_maps,
                kernel_size=(3, 3),
                padding='same',
            ),
            torch.nn.BatchNorm2d(num_features=intermediate_maps),
            torch.nn.LeakyReLU(),
            torch.nn.Conv2d(
                in_channels=intermediate_maps,
                out_channels=intermediate_maps,
                kernel_size=(3, 3),
                padding='same',
            ),
            torch.nn.BatchNorm2d(num_features=intermediate_maps),
            torch.nn.LeakyReLU(),
            torch.nn.Conv2d(
                in_channels=intermediate_maps,
                out_channels=intermediate_maps,
                kernel_size=(3, 3),
                padding='same',
            ),
            torch.nn.BatchNorm2d(num_features=intermediate_maps),
            torch.nn.LeakyReLU(),
            torch.nn.Conv2d(
                in_channels=intermediate_maps,
                out_channels=intermediate_maps,
                kernel_size=(3, 3),
                padding='same',
            ),
            torch.nn.BatchNorm2d(num_features=intermediate_maps),
            torch.nn.LeakyReLU(),
            torch.nn.Flatten(),
            torch.nn.Linear(
                int(np.prod(input_shape[2:])) * intermediate_maps,
                output_dim,
            )
        ])

    return c_extractor_arch
