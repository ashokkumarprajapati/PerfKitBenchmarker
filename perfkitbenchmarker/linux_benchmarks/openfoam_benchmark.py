# Copyright 2019 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""OpenFOAM Benchmark.

OpenFOAM is a C++ toolbox for the development of customized numerical solvers,
and pre-/post-processing utilities for the solution of continuum mechanics
problems, most prominently including computational fluid dynamics.
https://openfoam.org/

This benchmark runs a motorbike simulation that is popularly used to measure
scalability of OpenFOAM across multiple cores. Since this is a complex
computation, make sure to use a compute-focused machine-type that has multiple
cores before attempting to run.

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import posixpath
import re

from perfkitbenchmarker import configs
from perfkitbenchmarker import flags
from perfkitbenchmarker import hpc_util
from perfkitbenchmarker import sample
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.linux_packages import openfoam


_DEFAULT_CASE = 'motorbike'
_CASE_PATHS = {
    'motorbike': 'tutorials/incompressible/simpleFoam/motorBike',
    'pipe_cyclic': 'tutorials/incompressible/simpleFoam/pipeCyclic',
}
assert _DEFAULT_CASE in _CASE_PATHS

FLAGS = flags.FLAGS
flags.DEFINE_enum(
    'openfoam_case', _DEFAULT_CASE,
    sorted(list(_CASE_PATHS.keys())),
    'Name of the OpenFOAM case to run.')

# Convenience flag when running motorbike. Motorbike is the most common case,
# so we provide different sizes here.
_DEFAULT_MOTORBIKE_DIMENSIONS = 'small'
_MOTORBIKE_DIMENSIONS = {
    'small': '20 8 8',
    'medium': '40 16 16',
    'large': '80 32 32',
    'x-large': '160 64 64',
}
assert _DEFAULT_MOTORBIKE_DIMENSIONS in _MOTORBIKE_DIMENSIONS
flags.DEFINE_enum(
    'openfoam_motorbike_dimensions', _DEFAULT_MOTORBIKE_DIMENSIONS,
    sorted(list(_MOTORBIKE_DIMENSIONS.keys())),
    'If running motorbike, sets the dimensions of the motorbike case.')

# Problem size and scaling
flags.DEFINE_string(
    'openfoam_dimensions', _MOTORBIKE_DIMENSIONS[_DEFAULT_MOTORBIKE_DIMENSIONS],
    'Dimensions of the case.')
flags.DEFINE_integer(
    'openfoam_num_threads', None,
    'The number of threads to run OpenFOAM with.')
flags.DEFINE_string(
    'openfoam_mpi_mapping', 'core:SPAN',
    'Mpirun process mapping to use as arguments to "mpirun --map-by".')


BENCHMARK_NAME = 'openfoam'
_BENCHMARK_ROOT = '$HOME/OpenFOAM/run'
BENCHMARK_CONFIG = """
openfoam:
  description: Runs an OpenFOAM benchmark.
  vm_groups:
    default:
      vm_spec:
        GCP:
          machine_type: c2-standard-8
          zone: us-east1-c
          boot_disk_size: 100
        Azure:
          machine_type: Standard_F8s_v2
          zone: eastus2
          boot_disk_size: 100
        AWS:
          machine_type: c5.2xlarge
          zone: us-east-1f
          boot_disk_size: 100
      os_type: ubuntu1604
      vm_count: 2
      disk_spec:
        GCP:
          disk_type: nfs
          nfs_managed: False
          mount_point: {path}
        Azure:
          disk_type: nfs
          nfs_managed: False
          mount_point: {path}
        AWS:
          disk_type: nfs
          nfs_managed: False
          mount_point: {path}
""".format(path=_BENCHMARK_ROOT)
_MACHINEFILE = 'MACHINEFILE'
_RUNSCRIPT = 'Allrun'
_DECOMPOSEDICT = 'system/decomposeParDict'
_BLOCKMESHDICT = 'system/blockMeshDict'

_TIME_RE = re.compile(r"""(\d+)m       # The minutes part
                          (\d+)\.\d+s  # The seconds part """, re.VERBOSE)

_SSH_CONFIG_CMD = 'echo "LogLevel ERROR" | tee -a $HOME/.ssh/config'


def GetConfig(user_config):
  """Returns the configuration of a benchmark."""
  config = configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)
  if FLAGS['num_vms'].present:
    config['vm_groups']['default']['vm_count'] = FLAGS.num_vms
  return config


def Prepare(benchmark_spec):
  """Prepares the VMs and other resources for running the benchmark.

  This is a good place to download binaries onto the VMs, create any data files
  needed for a benchmark run, etc.

  Args:
    benchmark_spec: The benchmark spec for this sample benchmark.
  """
  vms = benchmark_spec.vms
  vm_util.RunThreaded(lambda vm: vm.Install('openfoam'), vms)

  master_vm = vms[0]
  master_vm.RemoteCommand('mkdir -p %s' % _BENCHMARK_ROOT)
  master_vm.RemoteCommand('cp -r {case_path} {run_path}'.format(
      case_path=posixpath.join(openfoam.OPENFOAM_ROOT,
                               _CASE_PATHS[FLAGS.openfoam_case]),
      run_path=_BENCHMARK_ROOT))

  if len(vms) > 1:
    # Allow ssh access to other vms and avoid printing ssh warnings when running
    # mpirun.
    vm_util.RunThreaded(lambda vm: vm.AuthenticateVm(), vms)
    vm_util.RunThreaded(lambda vm: vm.RemoteCommand(_SSH_CONFIG_CMD), vms)
    # Tells mpirun about other nodes
    hpc_util.CreateMachineFile(vms, remote_path=_GetPath(_MACHINEFILE))


def _AsSeconds(input_time):
  """Convert time from formatted string to seconds.

  Input format: 200m1.419s
  Should return 1201

  Args:
    input_time: The time to parse to an integer.

  Returns:
    An integer representing the time in seconds.
  """
  match = _TIME_RE.match(input_time)
  assert match, 'Time "{}" does not match format "{}"'.format(input_time,
                                                              _TIME_RE.pattern)
  minutes, seconds = match.group(1, 2)
  return int(minutes) * 60 + int(seconds)


def _GetSample(line):
  """Parse a single output line into a performance sample.

  Input format:
    real    4m1.419s

  Args:
    line: A single line from the OpenFOAM timing output.

  Returns:
    A single performance sample, with times in ms.
  """
  runtime_category, runtime_output = line.split()
  runtime_seconds = _AsSeconds(runtime_output)
  logging.info('Runtime of %s seconds from [%s, %s]',
               runtime_seconds, runtime_category, runtime_output)
  runtime_category = 'time_' + runtime_category
  return sample.Sample(runtime_category, runtime_seconds, 'seconds')


def _GetSamples(output):
  """Parse the output and return performance samples.

  Output is in the format:
    real    4m1.419s
    user    23m11.198s
    sys     0m25.274s

  Args:
    output: The output from running the OpenFOAM benchmark.

  Returns:
    A list of performance samples.
  """
  return [_GetSample(line) for line in output.strip().splitlines()]


def _GetOpenfoamVersion(vm):
  """Get the installed OpenFOAM version from the vm."""
  return vm.RemoteCommand('echo $WM_PROJECT_VERSION')[0]


def _GetOpenmpiVersion(vm):
  """Get the installed OpenMPI version from the vm."""
  return vm.RemoteCommand('mpirun -version')[0].split()[3]


def _GetWorkingDirPath():
  """Get the base directory name of the case being run."""
  case_dir_name = posixpath.basename(_CASE_PATHS[FLAGS.openfoam_case])
  return posixpath.join(_BENCHMARK_ROOT, case_dir_name)


def _GetPath(openfoam_file):
  """Get the absolute path to the file in the working directory."""
  return posixpath.join(_GetWorkingDirPath(), openfoam_file)


def _SetDecomposeMethod(vm, decompose_method):
  """Set the parallel decomposition method if using multiple cores."""
  logging.info('Using %s decomposition', decompose_method)
  vm_util.ReplaceText(vm, 'method.*', 'method %s;' % decompose_method,
                      _GetPath(_DECOMPOSEDICT))


def _SetNumProcesses(vm, num_processes):
  """Configure OpenFOAM to use the correct number of processes."""
  logging.info('Decomposing into %s subdomains', num_processes)
  vm_util.ReplaceText(vm, 'numberOfSubdomains.*',
                      'numberOfSubdomains %s;' % str(num_processes),
                      _GetPath(_DECOMPOSEDICT))


def _SetDimensions(vm, dimensions):
  """Sets the mesh dimensions in blockMeshDict.

  Replaces lines of the format:
  hex (0 1 2 3 4 5 6 7) (20 8 8) simpleGrading (1 1 1)

  with:
  hex (0 1 2 3 4 5 6 7) (dimensions) simpleGrading (1 1 1)

  Args:
    vm: The vm to make the replacement on.
    dimensions: String, new mesh dimensions to run with.

  """
  logging.info('Using dimensions (%s) in blockMeshDict', dimensions)
  vm_util.ReplaceText(vm, r'(hex \(.*\) \().*(\) .* \(.*\))',
                      r'\1{}\2'.format(dimensions),
                      _GetPath(_BLOCKMESHDICT),
                      regex_char='|')


def _UseMpi(vm, num_processes):
  """Configure OpenFOAM to use MPI if running with more than 1 VM."""
  runscript = _GetPath(_RUNSCRIPT)
  vm_util.ReplaceText(
      vm, 'runParallel', 'mpirun '
      '-hostfile {machinefile} '
      '-mca btl ^openib '
      '--map-by {mapping} '
      '-np {num_processes}'.format(
          machinefile=_GetPath(_MACHINEFILE),
          mapping=FLAGS.openfoam_mpi_mapping,
          num_processes=num_processes),
      runscript, '|')
  vm_util.ReplaceText(vm, '^mpirun.*', '& -parallel', runscript)


def Run(benchmark_spec):
  """Runs the benchmark and returns a dict of performance data.

  It must be possible to run the benchmark multiple times after the Prepare
  stage.

  Args:
    benchmark_spec: The benchmark spec for the OpenFOAM benchmark.

  Returns:
    A list of performance samples.
  """
  vms = benchmark_spec.vms
  master_vm = vms[0]
  num_vms = len(vms)
  num_cpus_available = num_vms * master_vm.NumCpusForBenchmark()
  num_cpus_to_use = FLAGS.openfoam_num_threads or num_cpus_available // 2
  logging.info('Running %s case on %s/%s cores on %s vms',
               FLAGS.openfoam_case,
               num_cpus_to_use,
               num_cpus_available,
               num_vms)

  # Configure the run
  case = FLAGS.openfoam_case
  master_vm.RemoteCommand('cp -r {case_path} {destination}'.format(
      case_path=posixpath.join(openfoam.OPENFOAM_ROOT, _CASE_PATHS[case]),
      destination=_BENCHMARK_ROOT))
  if case == 'motorbike':
    dimensions = _MOTORBIKE_DIMENSIONS[FLAGS.openfoam_motorbike_dimensions]
  if FLAGS['openfoam_dimensions'].present:
    dimensions = FLAGS.openfoam_dimensions
  _SetDimensions(master_vm, dimensions)
  _SetDecomposeMethod(master_vm, 'scotch')
  _SetNumProcesses(master_vm, num_cpus_to_use)
  if num_vms > 1:
    _UseMpi(master_vm, num_cpus_to_use)

  # Run and collect samples
  run_command = ' && '.join([
      'cd %s' % _GetWorkingDirPath(),
      './Allclean',
      'time ./Allrun'
  ])
  _, run_output = master_vm.RemoteCommand(run_command)
  samples = _GetSamples(run_output)
  common_metadata = {
      'case_name': FLAGS.openfoam_case,
      'dimensions': dimensions,
      'total_cpus_available': num_cpus_available,
      'total_cpus_used': num_cpus_to_use,
      'openfoam_version': _GetOpenfoamVersion(master_vm),
      'openmpi_version': _GetOpenmpiVersion(master_vm),
      'mpi_mapping': FLAGS.openfoam_mpi_mapping,
  }
  for result in samples:
    result.metadata.update(common_metadata)
  return samples


def Cleanup(benchmark_spec):
  """Cleans up after the benchmark completes.

  The state of the VMs should be equivalent to the state before Prepare was
  called.

  Args:
    benchmark_spec: The benchmark spec for the OpenFOAM benchmark.
  """
  del benchmark_spec
