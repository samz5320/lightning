# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import time
from multiprocessing.queues import SimpleQueue
from typing import Any, Callable, Optional, TYPE_CHECKING

import torch.multiprocessing as mp

import pytorch_lightning as pl
from pytorch_lightning.strategies.launchers.spawn import _FakeQueue, _SpawnLauncher, _SpawnOutput
from pytorch_lightning.trainer.states import TrainerFn
from pytorch_lightning.utilities import _TPU_AVAILABLE
from pytorch_lightning.utilities.apply_func import move_data_to_device
from pytorch_lightning.utilities.rank_zero import rank_zero_debug

if _TPU_AVAILABLE:
    import torch_xla.distributed.xla_multiprocessing as xmp
else:
    xm, xmp, MpDeviceLoader, rendezvous = [None] * 4

if TYPE_CHECKING:
    from pytorch_lightning.strategies import Strategy


class _XLASpawnLauncher(_SpawnLauncher):
    r"""Spawns processes that run a given function in parallel on XLA supported hardware, and joins them all at the end.

    The main process in which this launcher is invoked creates N so-called worker processes (using the
    `torch_xla` :func:`xmp.spawn`) that run the given function.
    Worker processes have a rank that ranges from 0 to N - 1.

    Note:
        - This launcher requires all objects to be pickleable.
        - It is important that the entry point to the program/script is guarded by ``if __name__ == "__main__"``.

    Args:
        strategy: A reference to the strategy that is used together with this launcher
    """

    def __init__(self, strategy: "Strategy") -> None:
        super().__init__(strategy)
        self._start_method = "fork"

    @property
    def is_interactive_compatible(self) -> bool:
        return True

    def launch(self, function: Callable, *args: Any, trainer: Optional["pl.Trainer"] = None, **kwargs: Any) -> Any:
        """Spawns processes that run the given function in parallel.

        The function is allowed to have a return value. However, when all processes join, only the return value
        of worker process 0 gets returned from this `launch` method in the main process.

        Arguments:
            function: The entry point for all spawned processes.
            *args: Optional positional arguments to be passed to the given function.
            trainer: Optional reference to the :class:`~pytorch_lightning.trainer.trainer.Trainer` for which
                a selected set of attributes get restored in the main process after processes join.
            **kwargs: Optional keyword arguments to be passed to the given function.
        """
        context = mp.get_context(self._start_method)
        return_queue = context.SimpleQueue()
        xmp.spawn(
            self._wrapping_function,
            args=(trainer, function, args, kwargs, return_queue),
            nprocs=len(self._strategy.parallel_devices),
            start_method=self._start_method,
        )
        spawn_output = return_queue.get()
        if trainer is None:
            return spawn_output

        self._recover_results_in_main_process(spawn_output, trainer)
        return spawn_output.trainer_results

    def _wrapping_function(
        self,
        process_idx: int,
        trainer: Optional["pl.Trainer"],
        function: Callable,
        args: Any,
        kwargs: Any,
        return_queue: SimpleQueue,
    ) -> None:
        self._strategy._worker_setup(process_idx)
        results = function(*args, **kwargs)

        if trainer is not None:
            results = self._collect_rank_zero_results(trainer, results)

        if self._strategy.local_rank == 0:
            return_queue.put(move_data_to_device(results, "cpu"))

        # https://github.com/pytorch/xla/issues/1801#issuecomment-602799542
        self._strategy.barrier("end-process")

        # Ensure that the rank 0 process is the one exiting last
        # https://github.com/pytorch/xla/issues/2190#issuecomment-641665358
        if self._strategy.local_rank == 0:
            time.sleep(2)

    def _collect_rank_zero_results(self, trainer: "pl.Trainer", results: Any) -> Optional["_SpawnOutput"]:
        rank_zero_debug("Finalizing the TPU spawn environment.")
        checkpoint_callback = trainer.checkpoint_callback
        best_model_path = (
            checkpoint_callback.best_model_path
            if checkpoint_callback and hasattr(checkpoint_callback, "best_model_path")
            else None
        )

        # requires to compute the state_dict on all processes in case Metrics are present
        state_dict = trainer.lightning_module.state_dict()

        # save the last weights
        weights_path = None
        if trainer.state.fn == TrainerFn.FITTING:
            weights_path = os.path.join(trainer.default_root_dir, ".temp.ckpt")
            self._strategy.checkpoint_io.save_checkpoint(state_dict, weights_path)

        # We use `local_rank` here as separate filesystems are used for each VM for TPU Pod Training
        if self._strategy.local_rank != 0:
            return None

        # adds the `callback_metrics` to the queue
        extra = _FakeQueue()
        self.add_to_queue(trainer, extra)

        return _SpawnOutput(best_model_path, weights_path, trainer.state, results, extra)
