"""
Orchestrating the dwi-preprocessing workflow
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. autofunction:: init_qsiprep_dwi_preproc_wf

"""

import os

import nibabel as nb
from nipype import logging

from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu

from ...interfaces import DerivativesDataSink

from ...interfaces.reports import DiffusionSummary, GradientPlot
from ...interfaces.images import SplitDWIs, ConcatRPESplits
from ...interfaces.gradients import SliceQC
from ...interfaces.confounds import DMRISummary
from ...interfaces.mrtrix import MRTrixGradientTable
from ...engine import Workflow

# dwi workflows
from ..fieldmap.bidirectional_pepolar import init_bidirectional_b0_unwarping_wf
from ..fieldmap.base import init_sdc_wf
from ..fieldmap.unwarp import init_fmap_unwarp_report_wf
from .merge import init_merge_and_denoise_wf
from .hmc import init_dwi_hmc_wf
from .util import init_dwi_reference_wf, _create_mem_gb, _get_wf_name, _get_first
from .registration import init_b0_to_anat_registration_wf
from .resampling import init_dwi_trans_wf
from .confounds import init_dwi_confs_wf
from .derivatives import init_dwi_derivatives_wf

DEFAULT_MEMORY_MIN_GB = 0.01
LOGGER = logging.getLogger('nipype.workflow')


def init_dwi_pre_hmc_wf(dwi_series,
                        rpe_series,
                        dwi_series_pedir,
                        dwi_denoise_window,
                        denoise_before_combining,
                        omp_nthreads,
                        low_mem,
                        name="pre_hmc_wf"):
    """
    This workflow controls the dwi initial stages of the dwi pipeline. Denoising
    must occur before any interpolation. The outputs from this workflow are
    lists of single volumes (optionally denoised) and corresponding lists of
    bvals, bvecs, etc.

    .. workflow::
        :graph2use: orig
        :simple_form: yes

        from qsiprep.workflows.dwi.base import init_qsiprep_dwi_preproc_wf
        wf = init_dwi_pre_hmc_wf(['/completely/made/up/path/sub-01_dwi.nii.gz'],
                                  omp_nthreads=1,
                                  dwi_denoise_window=7,
                                  denoise_before_combining=True,
                                  low_mem=False)

    **Parameters**

        dwi_series : list
            List of dwi series NIfTI files to be combined or a dict of PE-dir -> files
        rpe_series : list
            List of dwi series NIfTI files with the reverse phase encoding direction of
            those in  ``dwi_series``
        dwi_denoise_window : int
            window size in voxels for ``dwidenoise``. Must be odd. If 0, '
            '``dwidwenoise`` will not be run'
        denoise_before_combining : bool
            'run ``dwidenoise`` before combining dwis. Requires ``combine_all_dwis``'
        omp_nthreads : int
            Maximum number of threads an individual process may use
        low_mem : bool
            Write uncompressed .nii files in some cases to reduce memory usage

    **Outputs**
        dwi_files
            list of (potentially-denoised) single-volume dwi files
        bvec_files
            list of single-volume bvec files
        bval_files
            list of single-volume bval files
        b0_indices
            list of the positions of the b0 images in the dwi series
        b0_images
            list of paths to single-volume b0 images
        original_files
            list of paths to the original files that the single volumes came from
    """
    workflow = Workflow(name=name)
    outputnode = pe.Node(
        niu.IdentityInterface(fields=[
            'dwi_files', 'bval_files', 'bvec_files', 'original_files',
            'b0_images', 'b0_indices', 'rpe_b0s']),
        name='outputnode')

    doing_bidirectional_pepolar = len(rpe_series) > 0

    # Special case: Two reverse PE DWI series
    if doing_bidirectional_pepolar:
        # Merge, denoise, split, hmc on the plus series
        plus_files, minus_files = (rpe_series, dwi_series) if dwi_series_pedir.endswith("-") \
                                  else (dwi_series, rpe_series)
        merge_plus = init_merge_and_denoise_wf(dwi_denoise_window=dwi_denoise_window,
                                               denoise_before_combining=denoise_before_combining,
                                               name="merge_plus")
        split_plus = pe.Node(SplitDWIs(), name="split_plus")
        merge_plus.inputs.inputnode.dwi_files = plus_files

        # Merge, denoise, split, hmc on the minus series
        merge_minus = init_merge_and_denoise_wf(dwi_denoise_window=dwi_denoise_window,
                                                denoise_before_combining=denoise_before_combining,
                                                name="merge_minus")
        split_minus = pe.Node(SplitDWIs(), name="split_minus")
        merge_minus.inputs.inputnode.dwi_files = minus_files

        concat_rpe_splits = pe.Node(ConcatRPESplits(), name="concat_rpe_splits")

        # Combine the original images from the splits into one 'Split'
        workflow.connect([
            # Merge, denoise, split on the plus series
            (merge_plus, split_plus, [('outputnode.merged_image', 'dwi_file'),
                                      ('outputnode.merged_bval', 'bval_file'),
                                      ('outputnode.merged_bvec', 'bvec_file')]),
            (split_plus, concat_rpe_splits, [
                ('bval_files', 'bval_plus'),
                ('bvec_files', 'bvec_plus'),
                ('dwi_files', 'dwi_plus'),
                ('b0_images', 'b0_images_plus'),
                ('b0_indices', 'b0_indices_plus'),
                ('original_files', 'original_files_plus')]),

            # Merge, denoise, split on the minus series
            (merge_minus, split_minus, [('outputnode.merged_image', 'dwi_file'),
                                        ('outputnode.merged_bval', 'bval_file'),
                                        ('outputnode.merged_bvec', 'bvec_file')]),
            (split_minus, concat_rpe_splits, [
                ('bval_files', 'bval_minus'),
                ('bvec_files', 'bvec_minus'),
                ('dwi_files', 'dwi_minus'),
                ('b0_images', 'b0_images_minus'),
                ('b0_indices', 'b0_indices_minus'),
                ('original_files', 'original_files_minus')]),

            # Connect to the outputnode
            (concat_rpe_splits, outputnode, [
                ('dwi_files', 'dwi_files'),
                ('bval_files', 'bval_files'),
                ('bvec_files', 'bvec_files'),
                ('original_files', 'original_files'),
                ('b0_images', 'b0_images'),
                ('b0_indices', 'b0_indices')])
            ])
        return workflow

    merge_dwis = init_merge_and_denoise_wf(dwi_denoise_window=dwi_denoise_window,
                                           denoise_before_combining=denoise_before_combining,
                                           name="merge_dwis")
    split_dwis = pe.Node(SplitDWIs(), name="split_dwis")
    merge_dwis.inputs.inputnode.dwi_files = dwi_series
    split_dwis = pe.Node(SplitDWIs(), name="split_dwis")

    workflow.connect([
        (merge_dwis, split_dwis, [
            ('outputnode.merged_image', 'dwi_file'),
            ('outputnode.merged_bval', 'bval_file'),
            ('outputnode.merged_bvec', 'bvec_file')]),
        (split_dwis, outputnode, [
            ('dwi_files', 'dwi_files'),
            ('bval_files', 'bval_files'),
            ('bvec_files', 'bvec_files'),
            ('original_files', 'original_files'),
            ('b0_images', 'b0_images'),
            ('b0_indices', 'b0_indices')])
    ])

    return workflow
