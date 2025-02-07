# -*- coding: utf-8 -*-

"""ALF dataset generation."""


#------------------------------------------------------------------------------
# Imports
#------------------------------------------------------------------------------

import logging
from pathlib import Path
import shutil
import ast
import uuid

from tqdm import tqdm
import numpy as np

from phylib.utils._misc import _read_tsv_simple, ensure_dir_exists
from phylib.io.array import _spikes_per_cluster, _unique
from phylib.io.model import load_model

logger = logging.getLogger(__name__)


#------------------------------------------------------------------------------
# File utils
#------------------------------------------------------------------------------

NCH_WAVEFORMS = 32  # number of channels to be saved in templates.waveforms and channels.waveforms

_FILE_RENAMES = [  # file_in, file_out, squeeze (bool to squeeze vector from matlab in npy)
    ('params.py', 'params.py', None),
    ('cluster_metrics.csv', 'clusters.metrics.csv', None),
    ('spike_clusters.npy', 'spikes.clusters.npy', True),
    ('spike_templates.npy', 'spikes.templates.npy', True),
    ('channel_positions.npy', 'channels.localCoordinates.npy', False),
    ('channel_probe.npy', 'channels.probes.npy', True),
    ('cluster_probes.npy', 'clusters.probes.npy', True),
    ('cluster_shanks.npy', 'clusters.shanks.npy', True),
    # ('cluster_group.tsv', 'ks2/clusters.phyAnnotation.tsv', False), # todo check indexing, add2QC
]

FILE_DELETES = [
    'temp_wh.dat',  # potentially large file that will clog the servers
]


def _read_npy_header(filename):
    d = {}
    with open(filename, 'rb') as fid:
        d['magic_string'] = fid.read(6)
        d['version'] = fid.read(2)
        d['len'] = int.from_bytes(fid.read(2), byteorder='little')
        d = {**d, **ast.literal_eval(fid.read(d['len']).decode())}
    return d


def _create_if_possible(path, new_path, force=False):
    """Prepare the copy/move/symlink of a file, by making sure the source exists
    while the destination does not."""
    if not Path(path).exists():  # pragma: no cover
        logger.warning("Path %s does not exist, skipping.", path)
        return False
    if Path(new_path).exists() and not force:  # pragma: no cover
        logger.warning("Path %s already exists, skipping.", new_path)
        return False
    ensure_dir_exists(new_path.parent)
    return True


def _copy_if_possible(path, new_path, force=False):
    if not _create_if_possible(path, new_path, force=force):
        return False
    logger.debug("Copying %s to %s.", path, new_path)
    shutil.copy(path, new_path)
    return True


def _load(path):
    path = str(path)
    if path.endswith('.npy'):
        return np.load(path)
    elif path.endswith(('.csv', '.tsv')):
        return _read_tsv_simple(path)[1]  # the function returns a tuple (field, data)
    elif path.endswith('.bin'):
        # TODO: configurable dtype
        return np.fromfile(path, np.int16)


#------------------------------------------------------------------------------
# Ephys ALF creator
#------------------------------------------------------------------------------

class EphysAlfCreator(object):
    """Class for converting a dataset in KS/phy format into ALF."""

    def __init__(self, model):
        self.model = model
        self.dir_path = Path(model.dir_path)
        self.spc = _spikes_per_cluster(model.spike_clusters)
        self.cluster_ids = _unique(self.model.spike_clusters)

    def convert(self, out_path, force=False, label='', ampfactor=1):
        """Convert from KS/phy format to ALF."""
        logger.info("Converting dataset to ALF.")
        self.out_path = Path(out_path)
        self.label = label
        self.ampfactor = ampfactor
        if self.out_path.resolve() == self.dir_path.resolve():
            raise IOError("The source and target directories cannot be the same.")
        if not self.out_path.exists():
            self.out_path.mkdir()

        with tqdm(desc="Converting to ALF", total=95) as bar:
            self.copy_files(force=force)
            bar.update(10)
            self.make_spike_times_amplitudes()
            bar.update(10)
            self.make_cluster_objects()
            bar.update(10)
            self.make_channel_objects()
            bar.update(5)
            self.make_depths()
            bar.update(20)
            self.make_template_object()
            bar.update(30)
            self.rm_files()
            bar.update(10)
            self.rename_with_label()

        # Return the TemplateModel of the converted ALF dataset if the params.py file exists.
        params_path = self.out_path / 'params.py'
        if params_path.exists():
            return load_model(params_path)

    def copy_files(self, force=False):
        for fn0, fn1, squeeze in _FILE_RENAMES:
            f0 = self.dir_path / fn0
            f1 = self.out_path / fn1
            _copy_if_possible(f0, f1, force=force)
            if f0.exists() and squeeze and f0.suffix == '.npy':
                h = _read_npy_header(f0)
                # ks2 outputs vectors as multidimensional arrays. If there is no distinction
                # for Matlab, there is one in Numpy
                if len(h['shape']) == 2 and h['shape'][-1] == 1:
                    d = np.load(f0)
                    np.save(f1, d.squeeze())
                    continue

    def rm_files(self):
        for fn0 in FILE_DELETES:
            fn = self.dir_path.joinpath(fn0)
            if fn.exists():  # pragma: no cover
                fn.unlink()

    # File creation
    # -------------------------------------------------------------------------

    def _save_npy(self, filename, arr):
        """Save an array into a .npy file."""
        np.save(self.out_path / filename, arr)

    def make_spike_times_amplitudes(self):
        """We cannot just rename/copy spike_times.npy because it is in unit of
        *samples*, and not in seconds."""
        self._save_npy('spikes.times.npy', self.model.spike_times)
        self._save_npy('spikes.samples.npy', self.model.spike_samples)
        self._save_npy('spikes.amps.npy', self.model.amplitudes * self.ampfactor)

    def make_cluster_objects(self):
        """Create clusters.channels, clusters.waveformsDuration and clusters.amps"""

        peak_channel_path = self.dir_path / 'clusters.channels.npy'
        if not peak_channel_path.exists():
            self._save_npy(peak_channel_path.name, self.model.templates_channels)

        waveform_duration_path = self.dir_path / 'clusters.peakToTrough.npy'
        if not waveform_duration_path.exists():
            self._save_npy(waveform_duration_path.name, self.model.templates_waveforms_durations)

        # group by average over cluster number
        camps = np.zeros(np.max(self.cluster_ids) - np.min(self.cluster_ids) + 1,) * np.nan
        camps[self.cluster_ids - np.min(self.cluster_ids)] = self.model.templates_amplitudes
        amps_path = self.dir_path / 'clusters.amps.npy'
        self._save_npy(amps_path.name, camps * self.ampfactor)

        # clusters uuids
        uuid_list = ['uuids']
        uuid_list.extend([str(uuid.uuid4()) for _ in range(camps.size)])
        with open(self.out_path / 'clusters.uuids.csv', 'w+') as fid:
            fid.write('\n'.join(uuid_list))

    def make_channel_objects(self):
        """If there is no rawInd file, create it"""
        rawInd_path = self.dir_path / 'channels.rawInd.npy'
        rawInd = np.zeros_like(self.model.channel_probes).astype(np.int)
        channel_offset = 0
        for probe in np.unique(self.model.channel_probes):
            ind = self.model.channel_probes == probe
            rawInd[ind] = self.model.channel_mapping[ind] - channel_offset
            channel_offset += np.max(self.model.channel_mapping[ind])
        self._save_npy(rawInd_path.name, rawInd)

    def make_depths(self):
        """Make spikes.depths.npy, clusters.depths.npy."""
        channel_positions = self.model.channel_positions
        assert channel_positions.ndim == 2

        spike_clusters = self.model.spike_clusters
        assert spike_clusters.ndim == 1

        cluster_channels = np.load(self.out_path / 'clusters.channels.npy')
        assert cluster_channels.ndim == 1
        n_clusters = cluster_channels.shape[0]

        clusters_depths = channel_positions[cluster_channels, 1]
        assert clusters_depths.shape == (n_clusters,)

        if self.model.sparse_features is None:
            spikes_depths = clusters_depths[spike_clusters]
        else:
            # if PC features are provided, compute the depth as the weighted sum of coordinates
            nbatch = 50000
            c = 0
            spikes_depths = np.zeros_like(self.model.spike_times)
            nspi = spikes_depths.shape[0]
            while True:
                ispi = np.arange(c, min(c + nbatch, nspi))
                # take only first component
                features = np.square(self.model.sparse_features.data[ispi, :, 0])
                ichannels = self.model.sparse_features.cols[self.model.spike_clusters[ispi]]
                ypos = self.model.channel_positions[ichannels, 1]

                spikes_depths[ispi] = np.sum(np.transpose(ypos * features) /
                                             np.sum(features, axis=1), axis=0)
                c += nbatch
                if c >= nspi:
                    break
        self._save_npy('spikes.depths.npy', spikes_depths)
        self._save_npy('clusters.depths.npy', clusters_depths)

    def make_template_object(self):
        """Creates the template waveforms sparse object
        Without manual curation, it also corresponds to clusters waveforms objects.
        """
        if self.model.sparse_templates.cols:
            raise(NotImplementedError("Sparse template export to ALF not implemented yet"))
        else:
            n_templates, n_wavsamps, nchall = self.model.sparse_templates.data.shape
            ncw = min(NCH_WAVEFORMS, nchall)  # for some datasets, 32 may be too much
            assert(n_templates == self.model.n_templates)
            templates = np.zeros((n_templates, n_wavsamps, ncw), dtype=np.float32)
            templates_inds = np.zeros((n_templates, ncw), dtype=np.int32)
            # for each template, find the nearest channels to keep (one the same probe...)
            for t in np.arange(n_templates):
                current_probe = self.model.channel_probes[self.model.templates_channels[t]]
                channel_distance = np.sum(np.abs(
                    self.model.channel_positions -
                    self.model.channel_positions[self.model.templates_channels[t]]), axis=1)
                channel_distance[self.model.channel_probes != current_probe] += np.inf
                templates_inds[t, :] = np.argsort(channel_distance)[:ncw]
                templates[t, ...] = self.model.sparse_templates.data[t, :][:, templates_inds[t, :]]
            np.save(self.out_path.joinpath('templates.waveforms'), templates * self.ampfactor)
            np.save(self.out_path.joinpath('templates.waveformsChannels'), templates_inds)
            np.save(self.out_path.joinpath('clusters.waveforms'), templates * self.ampfactor)
            np.save(self.out_path.joinpath('clusters.waveformsChannels'), templates_inds)

    def rename_with_label(self):
        """add the label as an ALF part name before the extension if any label provided"""
        if not self.label:
            return
        glob_patterns = ['channels.*', 'clusters.*', 'spikes.*', 'templates.*']
        for pattern in glob_patterns:
            for f in self.out_path.glob(pattern):
                f.rename(f.with_suffix(f'.{self.label}{f.suffix}'))
