# -*- coding: utf-8 -*-
"""
                                Batch sampler

Defines how to sample the streamlines available in the MultiSubjectData.

- Defines the __iter__ method:
    - Finds a list of streamlines ids and associated subj that you can later
    load in your favorite way.

    - It is possible to restrict the number of subjects in a batch (and thus
    the number of inputs to load associated with sampled streamlines), and to
    reduce the number of time we need to load new data by using the same
    subjects for a given number of "cycles".

USAGE:
Can be used in a torch DataLoader. For instance:
        # Initialize dataset
        dataset = MultiSubjectDataset(...)
        dataset.load_training_data()
        # Initialize batch sampler
        batch_sampler = BatchSampler(...)
        # Use this in the dataloader
        dataloader = DataLoader(dataset, batch_sampler=batch_sampler,
                                collate_fn=batch_loader.load_batch)
"""

import logging
from typing import List, Tuple, Iterator

import numpy as np
import torch
import torch.multiprocessing
from torch.utils.data import Sampler

from dwi_ml.experiment_utils.prints import TqdmLoggingHandler
from dwi_ml.data.dataset.multi_subject_containers import MultisubjectSubset


class DWIMLBatchSampler(Sampler):
    def __init__(self, dataset: MultisubjectSubset,
                 streamline_group_name: str, batch_size: int,
                 batch_size_units: str, nb_streamlines_per_chunk: int,
                 rng: int, nb_subjects_per_batch: int, cycles: int):
        """
        Parameters
        ----------
        dataset : MultisubjectSubset
            Dataset to sample from.
        streamline_group_name: str
            The name of the group to use to load the sequences among all
            streamline_groups in the data_source.
        batch_size : int
            Batch size. Can be defined in number of streamlines or in total
            length_mm (specified through batch_size_units).
        batch_size_units: str
            'nb_streamlines' or 'length_mm' (which should hopefully be
            correlated to the number of input data points).
        nb_streamlines_per_chunk: int
            In the case of a batch size in terms of 'length_mm', chunks of n
            streamlines are sampled at once, and then their size is checked,
            either removing streamlines if exceeded, or else sampling a new
            chunk of ids.
        rng : int
            Seed for the random number generator.
        nb_subjects_per_batch : int
            Maximum number of subjects to be used in a single batch. Depending
            on the model, this can avoid loading too many input volumes at the
            same time, for example. If None, always sample from all subjects.
        cycles : int
            Used if `nb_subjects_per_batch` is given. Number of batches
            re-using the same subjects (and thus the same volumes) before
            sampling new ones.
        """
        super().__init__(dataset)  # This does nothing but python likes it.

        # Checking that batch_size is correct
        if not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size (i.e. number of total timesteps in "
                             "the batch) should be a positive integeral "
                             "value, but got batch_size={}"
                             .format(batch_size))
        if batch_size_units not in ['nb_streamlines', 'length_mm']:
            raise ValueError("batch_size_unit should either be "
                             "'nb_streamlines' or 'length_mm'")
        if (batch_size_units == 'nb_streamlines' and
                nb_streamlines_per_chunk != batch_size):
            logging.warning("With a max_batch_size computed in terms of "
                            "number of streamlines, the chunk size is not "
                            "used. Ignored")

        # Checking that n_volumes was given if cycles was given
        if cycles and not nb_subjects_per_batch:
            raise ValueError("If `cycles` is defined, "
                             "`nb_subjects_per_batch` should be defined. Got: "
                             "nb_subjects_per_batch={}, cycles={}"
                             .format(nb_subjects_per_batch, cycles))

        # Batch sampler variables
        self.dataset = dataset
        self.streamline_group_name = streamline_group_name
        self.nb_subjects_per_batch = nb_subjects_per_batch
        self.cycles = cycles
        if batch_size_units == 'nb_streamlines':
            self.nb_streamlines_per_chunk = batch_size
        else:
            self.nb_streamlines_per_chunk = nb_streamlines_per_chunk
        self.batch_size = batch_size
        self.batch_size_units = batch_size_units

        # Find idx of streamline group
        self.streamline_group_idx = self.dataset.streamline_groups.index(
            self.streamline_group_name)

        # Set random numbers
        self.rng = rng
        self.np_rng = np.random.RandomState(self.rng)
        torch.manual_seed(self.rng)  # Set torch seed

        # Batch sampler's logging level can be changed separately from main
        # scripts.
        self.logger = logging.getLogger('batch_sampler_logger')
        self.logger.propagate = False
        self.logger.setLevel(logging.root.level)

    def make_logger_tqdm_fitted(self):
        """Possibility to use a tqdm-compatible logger in case the model
        is used through a tqdm progress bar."""
        self.logger.addHandler(TqdmLoggingHandler())

    @property
    def params(self):
        """
        All parameters. Contains at least all parameters that would be
        necessary to create this batch sampler again (except the dataset).
        """
        params = {
            'streamline_group_name': self.streamline_group_name,
            'batch_size': self.batch_size,
            'batch_size_units': self.batch_size_units,
            'nb_streamlines_per_chunk': self.nb_streamlines_per_chunk,
            'rng': self.rng,
            'nb_subjects_per_batch': self.nb_subjects_per_batch,
            'cycles': self.cycles,
            'type': type(self),
        }
        return params

    @property
    def states(self):
        states = {
            'rng_state': self.np_rng.get_state(),
        }
        return states

    def __iter__(self) -> Iterator[List[Tuple[int, list]]]:
        """
        Streamline sampling.

        First sample the subjects to be used from a given number of desired
        subjects, then sample streamline ids inside those volumes.

        Hint: To use this through the dataload, through a tqdm progress bar:
        with tqdm(dataloader) as pbar:
            train_iterator = enumerate(pbar)
            for batch_id, data in train_iterator:
                ...

        Returns
        -------
        batch_ids_per_subj : list[(int, list)]
            - The torch's dataloader will get this list and iterate on it, each
            time using __iter__ function of the dataset (a multisubjectSubset)
            to create a data and send it to the collate_fn (our load_batch).
            - This must be a list.
            - Inside the tuple, the int is the subject id and the list is
            the list of streamlines ids for this subject.
            - The list of streamline ids are relative ids inside each subject's
            tractogram).
        """
        # This is the list of all possible streamline ids
        global_streamlines_ids = np.arange(
            self.dataset.total_nb_streamlines[self.streamline_group_idx])
        ids_per_subjs = \
            self.dataset.streamline_ids_per_subj[self.streamline_group_idx]

        # This contains one bool per streamline:
        #   1 = this streamline has not been used yet.
        #   0 = this streamline has been used.
        global_unused_streamlines = np.ones_like(global_streamlines_ids)
        self.logger.debug("********* Entering batch sampler iteration!\n"
                          "          Choosing out of {} possible streamlines"
                          .format(sum(global_unused_streamlines)))

        # This will continue "yielding" batches until it encounters a break.
        # (i.e. when all streamlines have been used)
        while True:
            # Weight subjects by their number of remaining streamlines
            streamlines_per_subj = np.array(
                [np.sum(global_unused_streamlines[subj_id_slice])
                 for _, subj_id_slice in ids_per_subjs.items()])
            assert (np.sum(streamlines_per_subj) ==
                    np.sum(global_unused_streamlines)), \
                "Unexpected error, the total number of streamlines per " \
                "subject does not correspond to the total number of " \
                "streamlines in the multisubject dataset. Error in " \
                "streamline ids?"

            # Stopping if all streamlines have been used
            if np.sum(streamlines_per_subj) == 0:
                self.logger.info("No streamlines remain for this epoch, "
                                 "stopping...")
                break

            # Choose subjects from which to sample streamlines for the next
            # few cycles.
            if self.nb_subjects_per_batch:
                # Sampling first from subjects that were not seed a lot yet
                weights = streamlines_per_subj / np.sum(streamlines_per_subj)

                # Choosing only non-empty subjects
                nb_subjects = min(self.nb_subjects_per_batch,
                                  np.count_nonzero(weights))
                sampled_subjs = self.np_rng.choice(
                    np.arange(len(self.dataset.subjs_data_list)),
                    size=nb_subjects, replace=False, p=weights)
            else:
                # Sampling from all subjects
                sampled_subjs = ids_per_subjs.keys()
                nb_subjects = len(sampled_subjs)
            self.logger.debug('    Sampled subjects for the next few cycles: '
                              '{}'.format(sampled_subjs))

            # Final subject's batch size could be smaller if no streamlines are
            # left for this subject.
            max_batch_size_per_subj = self.batch_size / nb_subjects

            # Preparing to iterate on these chosen subjects for a predefined
            # number of cycles
            if self.cycles:
                iterator = range(self.cycles)
            else:
                # Infinite iterator, sampling from all subjects
                iterator = iter(int, 1)

            count_cycles = 0
            for _ in iterator:
                count_cycles += 1
                self.logger.debug(
                    "    Iteration for cycle # {}/{}."
                    .format(count_cycles,
                            self.cycles if self.cycles else 'inf'))

                batch_ids_per_subj = []
                for subj in sampled_subjs:
                    sampled_ids = self._sample_streamlines_for_subj(
                        subj, ids_per_subjs, global_unused_streamlines,
                        max_batch_size_per_subj)

                    # Append tuple (subj, list_sampled_ids) to the batch
                    batch_ids_per_subj.append((subj, sampled_ids))

                self.logger.debug(
                    "    Finished loop on subjects. Now yielding.\n"
                    "    (If this was called through a dataloader, it should "
                    "start using load_batch and even training \n"
                    "     on this batch while we prepare a batch for the next "
                    "cycle, if any).")

                if len(batch_ids_per_subj) == 0:
                    self.logger.debug(
                        "No more streamlines remain in any of the selected "
                        "volumes! Breaking now. You may call the next "
                        "iteration of this batch sampler!")
                    break

                yield batch_ids_per_subj

            # Finished cycle. Will choose new subjs if the number of iterations
            # is not reached for this __iter__ call.

    def _sample_streamlines_for_subj(self, subj, ids_per_subjs,
                                     global_unused_streamlines,
                                     max_batch_size_per_subj):
        """
        For each subject, randomly choose streamlines that have not been chosen
        yet.

        Params:
        ------
        subj: int
            The subject's id.
        ids_per_subjs: dict
            The list of this subject's streamlines' global ids.
        global_unused_streamlines: array
            One flag per global streamline id: 0 if already used, else 1.
        max_batch_size_per_subj:
            Max batch size to load for this subject.
        """
        sampled_ids = []

        # Get the global streamline ids corresponding to this
        # subject
        subj_slice = ids_per_subjs[subj]

        # We will continue iterating on this subject until we
        # break (i.e. when we reach the maximum batch size for this
        # subject)
        total_subj_batch_size = 0
        while True:
            # Add some more streamlines for this subject.
            (subbatch_global_ids, subbatch_rel_ids,
             subj_subbatch_size,
             no_streamlines_left, reached_max) = \
                self._get_a_chunk_of_streamlines(
                    subj_slice, global_unused_streamlines,
                    total_subj_batch_size, max_batch_size_per_subj)

            if no_streamlines_left:
                # No streamlines remaining. Get next subject.
                break

            if len(subbatch_rel_ids) == 0:
                logging.warning(
                    "MAJOR WARNING. Got no streamline for this subject in "
                    "this batch, but there are streamlines left. \n"
                    "Possibly means that the allowed batch size does not even "
                    "allow one streamline per batch.\n Check your batch size "
                    "choice!")

            # Mask the sampled streamlines
            global_unused_streamlines[subbatch_global_ids] = 0

            # Add sub-sampled ids to subject's batch
            sampled_ids.extend(subbatch_rel_ids)

            # Continue?
            if reached_max:
                # Batch size reached for this subject. Get next subject.
                break
            else:
                # Update size and get a new chunk
                total_subj_batch_size += subj_subbatch_size

        return sampled_ids

    def _get_a_chunk_of_streamlines(self, subj_slice,
                                    global_unused_streamlines,
                                    current_subbatch_size, max_subbatch_size):
        """
        Get a chunk of streamlines (for a given subject) and evaluate their
        size.

        Params
        ------
        subj_slice: slice
            All global streamline ids belonging to a given subject.
        global_unused_streamlines: array
            One flag per global streamline id: 0 if already used, else 1.
        current_subbatch_size: int
            Chunks's size + current_subbatch_size must not exceed
            max_subbatch_size.
        max_subbatch_size: int
            Maximum batch size for current subject.

        Returns:
        chosen_global_ids: list
            The list of global ids chosen for this chunk
        chosen_relative_ids:
            The same ids, but in terms of relative ids for current subject.
        no_streamlines_remaining: bool
            If true, all of this subject's streamlines have been used.
        reached_max_heaviness)
        """
        no_streamlines_remaining = False
        reached_max_heaviness = False

        # Find streamlines that have not been used yet for this subj
        subj_unused_ids_in_global = np.flatnonzero(
            global_unused_streamlines[subj_slice]) + subj_slice.start

        self.logger.debug("    Available streamlines for this subject: {}"
                          .format(len(subj_unused_ids_in_global)))

        # No streamlines remain for this subject
        if len(subj_unused_ids_in_global) == 0:
            no_streamlines_remaining = True
            return [], [], no_streamlines_remaining, reached_max_heaviness

        # Sample a chunk of streamlines
        chosen_global_ids = self.np_rng.choice(subj_unused_ids_in_global,
                                               self.nb_streamlines_per_chunk)

        # Compute chunk size and remove streamlines from it if necessary
        size_per_streamline = self._compute_and_adjust_batch_size(
            chosen_global_ids)

        # If batch_size has been exceeded, taking a little less streamlines
        # for this chunk.
        computed_chunk_size = np.sum(size_per_streamline)
        if current_subbatch_size + computed_chunk_size >= max_subbatch_size:
            cumulative_sum = np.cumsum(size_per_streamline)
            selected = cumulative_sum < (max_subbatch_size -
                                         current_subbatch_size)
            chosen_global_ids = chosen_global_ids[selected]
            size_per_streamline = size_per_streamline[selected]
            reached_max_heaviness = True

        computed_chunk_size = np.sum(size_per_streamline)
        self.logger.debug(
            "    Chunk_size was {} streamlines, but after verifying data "
            "(max batch size for this subj is {}), keeping only {} "
            "streamlines for a total of {} (units = {})."
            .format(self.nb_streamlines_per_chunk, max_subbatch_size,
                    len(chosen_global_ids), computed_chunk_size,
                    self.batch_size_units))

        # Fetch subject-relative ids
        chosen_relative_ids = list(chosen_global_ids - subj_slice.start)

        return (chosen_global_ids, chosen_relative_ids, computed_chunk_size,
                no_streamlines_remaining, reached_max_heaviness)

    def _compute_and_adjust_batch_size(self, chosen_global_ids):
        """
        Relying on the lengths_mm info available in the MultiSubjectData to be
        able to know the (eventual, if self.step_size) number of time steps
        without loading the streamlines, particularly with the lazy data.
        """
        if self.batch_size_units == 'length_mm':
            l_mm = self.dataset.streamline_lengths_mm
            l_mm = l_mm[self.streamline_group_idx][chosen_global_ids]
            size_per_streamline = l_mm
        else:  # units = nb_streamlines
            size_per_streamline = np.ones(len(chosen_global_ids))

        return size_per_streamline
