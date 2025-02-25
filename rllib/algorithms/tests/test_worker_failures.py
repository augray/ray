from collections import defaultdict
import gymnasium as gym
import numpy as np
import time
import unittest

import ray
from ray.experimental.state.api import list_actors
from ray.rllib.algorithms.algorithm_config import AlgorithmConfig
from ray.rllib.algorithms.a3c import A3CConfig
from ray.rllib.algorithms.apex_dqn import ApexDQNConfig
from ray.rllib.algorithms.callbacks import DefaultCallbacks
from ray.rllib.algorithms.dqn.dqn import DQNConfig
from ray.rllib.algorithms.impala import ImpalaConfig
from ray.rllib.algorithms.pg import PGConfig
from ray.rllib.algorithms.pg.pg_tf_policy import PGTF2Policy
from ray.rllib.algorithms.pg.pg_torch_policy import PGTorchPolicy
from ray.rllib.algorithms.ppo.ppo import PPOConfig
from ray.rllib.env.multi_agent_env import make_multi_agent
from ray.rllib.evaluation.rollout_worker import RolloutWorker
from ray.rllib.examples.env.random_env import RandomEnv
from ray.rllib.policy.policy import PolicySpec
from ray.rllib.utils.test_utils import framework_iterator
from ray.tune.registry import register_env


@ray.remote
class Counter:
    """Remote counter service that survives restarts."""

    def __init__(self):
        self.reset()

    def _key(self, eval, worker_index, vector_index):
        return f"{eval}:{worker_index}:{vector_index}"

    def increment(self, eval, worker_index, vector_index):
        self.counter[self._key(eval, worker_index, vector_index)] += 1

    def get(self, eval, worker_index, vector_index):
        return self.counter[self._key(eval, worker_index, vector_index)]

    def reset(self):
        self.counter = defaultdict(int)


class FaultInjectEnv(gym.Env):
    """Env that fails upon calling `step()`, but only for some remote worker indices.

    The worker indices that should produce the failure (a ValueError) can be
    provided by a list (of ints) under the "bad_indices" key in the env's
    config.

    Examples:
        >>> from ray.rllib.env.env_context import EnvContext
        >>> # This env will fail for workers 1 and 2 (not for the local worker
        >>> # or any others with an index != [1|2]).
        >>> bad_env = FaultInjectEnv(
        ...     EnvContext(
        ...         {"bad_indices": [1, 2]},
        ...         worker_index=1,
        ...         num_workers=3,
        ...      )
        ... )

        >>> from ray.rllib.env.env_context import EnvContext
        >>> # This env will fail only on the first evaluation worker, not on the first
        >>> # regular rollout worker.
        >>> bad_env = FaultInjectEnv(
        ...     EnvContext(
        ...         {"bad_indices": [1], "eval_only": True},
        ...         worker_index=2,
        ...         num_workers=5,
        ...     )
        ... )
    """

    def __init__(self, config):
        # Use RandomEnv to control episode length if needed.
        self.env = RandomEnv(config)
        self._skip_env_checking = True
        self.action_space = self.env.action_space
        self.observation_space = self.env.observation_space
        self.config = config
        # External counter service.
        if "counter" in config:
            self.counter = ray.get_actor(config["counter"])
        else:
            self.counter = None

        if (
            config.get("init_delay", 0) > 0.0
            and (
                not config.get("init_delay_indices", [])
                or self.config.worker_index in config.get("init_delay_indices", [])
            )
            and
            # constructor delay can only happen for recreated actors.
            self._get_count() > 0
        ):
            # Simulate an initialization delay.
            time.sleep(config.get("init_delay"))

    def _increment_count(self):
        if self.counter:
            eval = self.config.get("evaluation", False)
            worker_index = self.config.worker_index
            vector_index = self.config.vector_index
            ray.wait([self.counter.increment.remote(eval, worker_index, vector_index)])

    def _get_count(self):
        if self.counter:
            eval = self.config.get("evaluation", False)
            worker_index = self.config.worker_index
            vector_index = self.config.vector_index
            return ray.get(self.counter.get.remote(eval, worker_index, vector_index))
        return -1

    def _maybe_raise_error(self):
        # Do not raise simulated error if this worker is not bad.
        if self.config.worker_index not in self.config.get("bad_indices", []):
            return

        if self.counter:
            count = self._get_count()
            if self.config.get(
                "failure_start_count", -1
            ) >= 0 and count < self.config.get("failure_start_count"):
                return

            if self.config.get(
                "failure_stop_count", -1
            ) >= 0 and count >= self.config.get("failure_stop_count"):
                return

        raise ValueError(
            "This is a simulated error from "
            f"{'eval-' if self.config.get('evaluation', False) else ''}"
            f"worker-idx={self.config.worker_index}!"
        )

    def reset(self, *, seed=None, options=None):
        self._increment_count()
        self._maybe_raise_error()
        return self.env.reset()

    def step(self, action):
        self._increment_count()
        self._maybe_raise_error()

        if self.config.get("step_delay", 0) > 0.0 and (
            not self.config.get("init_delay_indices", [])
            or self.config.worker_index in self.config.get("step_delay_indices", [])
        ):
            # Simulate a step delay.
            time.sleep(self.config.get("step_delay"))

        return self.env.step(action)

    def action_space_sample(self):
        return self.env.action_space.sample()


class ForwardHealthCheckToEnvWorker(RolloutWorker):
    """Configure RolloutWorker to error in specific condition is hard.

    So we take a short-cut, and simply forward ping() to env.sample().
    """

    def ping(self) -> str:
        # See if Env wants to throw error.
        _ = self.env.step(self.env.action_space_sample())
        # If there is no error raised from sample(), we simply reply pong.
        return super().ping()


def wait_for_restore(num_restarting_allowed=0):
    """Wait for Ray actor fault tolerence to restore all failed workers.

    Args:
        num_restarting_allowed: Number of actors that are allowed to be
            in "RESTARTING" state. This is because some actors may
            hang in __init__().
    """
    while True:
        states = [
            a["state"]
            for a in list_actors(
                filters=[("class_name", "=", "ForwardHealthCheckToEnvWorker")]
            )
        ]
        finished = True
        for s in states:
            # Wait till all actors are either "ALIVE" (restored),
            # or "DEAD" (cancelled. these actors are from other
            # finished test cases) or "RESTARTING" (being restored).
            if s not in ["ALIVE", "DEAD", "RESTARTING"]:
                finished = False
                break

        restarting = [s for s in states if s == "RESTARTING"]
        if len(restarting) > num_restarting_allowed:
            finished = False

        print("waiting ... ", states)
        if finished:
            break
        # Otherwise, wait a bit.
        time.sleep(0.5)


class TestWorkerFailures(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ray.init()

        register_env("fault_env", lambda c: FaultInjectEnv(c))
        register_env(
            "multi-agent-fault_env", lambda c: make_multi_agent(FaultInjectEnv)(c)
        )

    @classmethod
    def tearDownClass(cls) -> None:
        ray.shutdown()

    def _do_test_fault_ignore(self, config: AlgorithmConfig, fail_eval: bool = False):
        # Test fault handling
        config.num_rollout_workers = 2
        config.ignore_worker_failures = True
        config.recreate_failed_workers = False
        config.env = "fault_env"
        # Make worker idx=1 fail. Other workers will be ok.
        config.env_config = {
            "bad_indices": [1],
        }
        if fail_eval:
            config.evaluation_num_workers = 2
            config.evaluation_interval = 1
            config.evaluation_config = {
                "ignore_worker_failures": True,
                "recreate_failed_workers": False,
                "env_config": {
                    # Make worker idx=1 fail. Other workers will be ok.
                    "bad_indices": [1],
                    "evaluation": True,
                },
            }

        print(config)

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            algo = config.build()
            algo.train()

            # One of the rollout workers failed.
            self.assertEqual(algo.workers.num_healthy_remote_workers(), 1)
            if fail_eval:
                # One of the eval workers failed.
                self.assertEqual(
                    algo.evaluation_workers.num_healthy_remote_workers(), 1
                )

            algo.stop()

    def _do_test_fault_fatal(self, config, fail_eval=False):
        # Test raises real error when out of workers.
        config.num_rollout_workers = 2
        config.env = "fault_env"
        # Make both worker idx=1 and 2 fail.
        config.env_config = {"bad_indices": [1, 2]}
        if fail_eval:
            config.evaluation_num_workers = 2
            config.evaluation_interval = 1
            config.evaluation_config = {
                # Make eval worker (index 1) fail.
                "env_config": {
                    "bad_indices": [1],
                    "evaluation": True,
                },
            }

        for _ in framework_iterator(config, frameworks=("torch", "tf")):
            a = config.build()
            self.assertRaises(Exception, lambda: a.train())
            a.stop()

    def _do_test_fault_fatal_but_recreate(self, config):
        # Counter that will survive restarts.
        COUNTER_NAME = "_do_test_fault_fatal_but_recreate"
        counter = Counter.options(name=COUNTER_NAME).remote()

        # Test raises real error when out of workers.
        config.num_rollout_workers = 1
        config.evaluation_num_workers = 1
        config.evaluation_interval = 1
        config.env = "fault_env"
        config.evaluation_config = {
            "recreate_failed_workers": True,
            # 0 delay for testing purposes.
            "delay_between_worker_restarts_s": 0,
            # Make eval worker (index 1) fail.
            "env_config": {
                "bad_indices": [1],
                "failure_start_count": 3,
                "failure_stop_count": 4,
                "counter": COUNTER_NAME,
            },
        }

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaction counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            a.train()
            wait_for_restore()
            a.train()

            self.assertEqual(a.workers.num_healthy_remote_workers(), 1)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 1)

            # This should also work several times.
            a.train()
            wait_for_restore()
            a.train()

            self.assertEqual(a.workers.num_healthy_remote_workers(), 1)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 1)

            a.stop()

    def test_fatal(self):
        # Test the case where all workers fail (w/o recovery).
        self._do_test_fault_fatal(PGConfig().training(optimizer={}))

    def test_async_grads(self):
        self._do_test_fault_ignore(
            A3CConfig()
            .training(optimizer={"grads_per_step": 1})
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

    def test_async_replay(self):
        config = (
            ApexDQNConfig()
            .training(
                optimizer={
                    "num_replay_buffer_shards": 1,
                },
            )
            .rollouts(
                num_rollout_workers=2,
            )
            .reporting(
                min_sample_timesteps_per_iteration=1000,
                min_time_s_per_iteration=1,
            )
            .resources(num_gpus=0)
            .exploration(explore=False)
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )
        config.target_network_update_freq = 100
        self._do_test_fault_ignore(config=config)

    def test_async_samples(self):
        self._do_test_fault_ignore(
            ImpalaConfig()
            .resources(num_gpus=0)
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

    def test_sync_replay(self):
        self._do_test_fault_ignore(
            DQNConfig()
            .reporting(min_sample_timesteps_per_iteration=1)
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

    def test_multi_g_p_u(self):
        self._do_test_fault_ignore(
            PPOConfig()
            .rollouts(rollout_fragment_length=5)
            .training(
                train_batch_size=10,
                sgd_minibatch_size=1,
                num_sgd_iter=1,
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

    def test_sync_samples(self):
        self._do_test_fault_ignore(
            PGConfig()
            .training(optimizer={})
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

    def test_async_sampling_option(self):
        self._do_test_fault_ignore(
            PGConfig()
            .rollouts(sample_async=True)
            .training(optimizer={})
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

    def test_eval_workers_failing_ignore(self):
        # Test the case where one eval worker fails, but we chose to ignore.
        self._do_test_fault_ignore(
            PGConfig()
            .training(model={"fcnet_hiddens": [4]})
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker),
            fail_eval=True,
        )

    def test_recreate_eval_workers_parallel_to_training_w_actor_manager(self):
        # Test the case where all eval workers fail, but we chose to recover.
        config = (
            PGConfig()
            .evaluation(
                evaluation_num_workers=1,
                enable_async_evaluation=True,
                evaluation_parallel_to_training=True,
                evaluation_duration="auto",
            )
            .training(model={"fcnet_hiddens": [4]})
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        self._do_test_fault_fatal_but_recreate(config)

    def test_eval_workers_failing_fatal(self):
        # Test the case where all eval workers fail (w/o recovery).
        self._do_test_fault_fatal(
            PGConfig().training(model={"fcnet_hiddens": [4]}),
            fail_eval=True,
        )

    def test_workers_fatal_but_recover(self):
        # Counter that will survive restarts.
        COUNTER_NAME = "test_workers_fatal_but_recover"
        counter = Counter.options(name=COUNTER_NAME).remote()

        config = (
            PGConfig()
            .rollouts(
                num_rollout_workers=2,
                rollout_fragment_length=16,
            )
            .training(
                train_batch_size=32,
                model={"fcnet_hiddens": [4]},
            )
            .environment(
                env="fault_env",
                env_config={
                    # Make both worker idx=1 and 2 fail.
                    "bad_indices": [1, 2],
                    "failure_start_count": 3,
                    "failure_stop_count": 4,
                    "counter": COUNTER_NAME,
                },
            )
            .fault_tolerance(
                recreate_failed_workers=True,  # But recover.
                # 0 delay for testing purposes.
                delay_between_worker_restarts_s=0,
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaciton counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            # Before training, 2 healthy workers.
            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            # Nothing is restarted.
            self.assertEqual(a.workers.num_remote_worker_restarts(), 0)

            a.train()
            wait_for_restore()
            # One more iteration. Workers will be recovered during this round.
            a.train()

            # After training, still 2 healthy workers.
            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            # Both workers are restarted.
            self.assertEqual(a.workers.num_remote_worker_restarts(), 2)

    def test_policies_are_restored_on_recovered_worker(self):
        class AddPolicyCallback(DefaultCallbacks):
            def __init__(self):
                super().__init__()

            def on_algorithm_init(self, *, algorithm, **kwargs):
                # Add a custom policy to algorithm
                algorithm.add_policy(
                    policy_id="test_policy",
                    policy_cls=(
                        PGTorchPolicy
                        if algorithm.config.framework_str == "torch"
                        else PGTF2Policy
                    ),
                    observation_space=gym.spaces.Box(low=0, high=1, shape=(8,)),
                    action_space=gym.spaces.Discrete(2),
                    config={},
                    policy_state=None,
                    evaluation_workers=True,
                )

        # Counter that will survive restarts.
        COUNTER_NAME = "test_policies_are_restored_on_recovered_worker"
        counter = Counter.options(name=COUNTER_NAME).remote()

        config = (
            PGConfig()
            .rollouts(
                num_rollout_workers=2,
                rollout_fragment_length=16,
            )
            .training(
                train_batch_size=32,
                model={"fcnet_hiddens": [4]},
            )
            .environment(
                env="multi-agent-fault_env",
                env_config={
                    # Make both worker idx=1 and 2 fail.
                    "bad_indices": [1, 2],
                    "failure_start_count": 3,
                    "failure_stop_count": 4,
                    "counter": COUNTER_NAME,
                },
            )
            .evaluation(
                evaluation_num_workers=1,
                evaluation_interval=1,
                evaluation_config=PGConfig.overrides(
                    recreate_failed_workers=True,
                    # Restart the entire eval worker.
                    restart_failed_sub_environments=False,
                    env_config={
                        "evaluation": True,
                        # Make eval worker (index 1) fail.
                        "bad_indices": [1],
                        "failure_start_count": 3,
                        "failure_stop_count": 4,
                        "counter": COUNTER_NAME,
                    },
                ),
            )
            .callbacks(callbacks_class=AddPolicyCallback)
            .fault_tolerance(
                recreate_failed_workers=True,  # But recover.
                # Throwing error in constructor is a bad idea.
                # 0 delay for testing purposes.
                delay_between_worker_restarts_s=0,
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaction counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            # Should have the custom policy.
            self.assertIsNotNone(a.get_policy("test_policy"))

            # Before train loop, workers are fresh and not recreated.
            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            self.assertEqual(a.workers.num_remote_worker_restarts(), 0)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 1)
            self.assertEqual(a.evaluation_workers.num_remote_worker_restarts(), 0)

            a.train()
            wait_for_restore()
            # One more iteration. Workers will be recovered during this round.
            a.train()

            # Everything still healthy. And all workers are restarted.
            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            self.assertEqual(a.workers.num_remote_worker_restarts(), 2)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 1)
            self.assertEqual(a.evaluation_workers.num_remote_worker_restarts(), 1)

            # Let's verify that our custom policy exists on both recovered workers.
            def has_test_policy(w):
                return "test_policy" in w.policy_map

            # Rollout worker has test policy.
            self.assertTrue(
                all(a.workers.foreach_worker(has_test_policy, local_worker=False))
            )
            # Eval worker has test policy.
            self.assertTrue(
                all(
                    a.evaluation_workers.foreach_worker(
                        has_test_policy, local_worker=False
                    )
                )
            )

    def test_eval_workers_fault_but_recover(self):
        # Counter that will survive restarts.
        COUNTER_NAME = "test_eval_workers_fault_but_recover"
        counter = Counter.options(name=COUNTER_NAME).remote()

        config = (
            PGConfig()
            .rollouts(
                num_rollout_workers=2,
                rollout_fragment_length=16,
            )
            .training(
                train_batch_size=32,
                model={"fcnet_hiddens": [4]},
            )
            .environment(env="fault_env")
            .evaluation(
                evaluation_num_workers=2,
                evaluation_interval=1,
                evaluation_config=PGConfig.overrides(
                    env_config={
                        "evaluation": True,
                        "p_terminated": 0.0,
                        "max_episode_len": 20,
                        # Make both eval workers fail.
                        "bad_indices": [1, 2],
                        # Env throws error between steps 10 and 12.
                        "failure_start_count": 3,
                        "failure_stop_count": 4,
                        "counter": COUNTER_NAME,
                    },
                ),
            )
            .fault_tolerance(
                recreate_failed_workers=True,  # And recover
                # 0 delay for testing purposes.
                delay_between_worker_restarts_s=0,
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaciton counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            # Before train loop, workers are fresh and not recreated.
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 2)
            self.assertEqual(a.evaluation_workers.num_remote_worker_restarts(), 0)

            a.train()
            wait_for_restore()
            a.train()

            # Everything still healthy. And all workers are restarted.
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 2)
            self.assertEqual(a.evaluation_workers.num_remote_worker_restarts(), 2)

    def test_worker_recover_with_hanging_workers(self):
        # Counter that will survive restarts.
        COUNTER_NAME = "test_eval_workers_fault_but_recover"
        counter = Counter.options(name=COUNTER_NAME).remote()

        config = (
            # Must use off-policy algorithm since we are gonna have hanging workers.
            ImpalaConfig()
            .resources(
                num_gpus=0,
            )
            .rollouts(
                num_rollout_workers=3,
                rollout_fragment_length=16,
            )
            .training(
                train_batch_size=32,
                model={"fcnet_hiddens": [4]},
            )
            .reporting(
                # Make sure each iteration doesn't take too long.
                min_time_s_per_iteration=0.5,
                # Make sure metrics reporting doesn't hang for too long
                # since we are gonna have a hanging worker.
                metrics_episode_collection_timeout_s=1,
            )
            .environment(
                env="fault_env",
                env_config={
                    "evaluation": True,
                    "p_terminated": 0.0,
                    "max_episode_len": 20,
                    # Worker 1 and 2 will fail in step().
                    "bad_indices": [1, 2],
                    # Env throws error between steps 3 and 4.
                    "failure_start_count": 3,
                    "failure_stop_count": 4,
                    "counter": COUNTER_NAME,
                    # Worker 2 will hang for long time during init after restart.
                    "init_delay": 3600,
                    "init_delay_indices": [2],
                    # Worker 3 will hang in env.step().
                    "step_delay": 3600,
                    "step_delay_indices": [3],
                },
            )
            .fault_tolerance(
                recreate_failed_workers=True,  # And recover
                worker_health_probe_timeout_s=0.01,
                worker_restore_timeout_s=5,
                delay_between_worker_restarts_s=0,  # For testing, no delay.
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaciton counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            # Before train loop, workers are fresh and not recreated.
            self.assertEqual(a.workers.num_healthy_remote_workers(), 3)
            self.assertEqual(a.workers.num_remote_worker_restarts(), 0)

            a.train()
            wait_for_restore(num_restarting_allowed=1)
            # Most importantly, training progressed fine.
            a.train()

            # 2 healthy remote workers left, although worker 3 is stuck in rollout.
            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            # Only 1 successful restore, since worker 2 is stuck in indefinite init
            # and can not be properly restored.
            self.assertEqual(a.workers.num_remote_worker_restarts(), 1)

    def test_eval_workers_fault_but_restore_env(self):
        # Counter that will survive restarts.
        COUNTER_NAME = "test_eval_workers_fault_but_restore_env"
        counter = Counter.options(name=COUNTER_NAME).remote()

        config = (
            PGConfig()
            .environment("fault_env")
            .rollouts(
                num_rollout_workers=2,
                rollout_fragment_length=16,
            )
            .training(
                train_batch_size=32,
                model={"fcnet_hiddens": [4]},
            )
            .environment(
                env="fault_env",
                env_config={
                    # Make both worker idx=1 and 2 fail.
                    "bad_indices": [1, 2],
                    "failure_start_count": 3,
                    "failure_stop_count": 4,
                    "counter": COUNTER_NAME,
                },
            )
            .evaluation(
                evaluation_num_workers=2,
                evaluation_interval=1,
                evaluation_config=PGConfig.overrides(
                    recreate_failed_workers=True,
                    # Now instead of recreating failed workers,
                    # we want to recreate the failed sub env instead.
                    restart_failed_sub_environments=True,
                    env_config={
                        "evaluation": True,
                        # Make eval worker (index 1) fail.
                        "bad_indices": [1],
                    },
                ),
            )
            .fault_tolerance(
                recreate_failed_workers=True,  # And recover
                # 0 delay for testing purposes.
                delay_between_worker_restarts_s=0,
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaciton counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            # Before train loop, workers are fresh and not recreated.
            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            self.assertEqual(a.workers.num_remote_worker_restarts(), 0)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 2)
            self.assertEqual(a.evaluation_workers.num_remote_worker_restarts(), 0)

            a.train()
            wait_for_restore()
            a.train()

            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            # Rollout workers were restarted.
            self.assertEqual(a.workers.num_remote_worker_restarts(), 2)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 2)
            # But eval workers were not restarted.
            self.assertEqual(a.evaluation_workers.num_remote_worker_restarts(), 0)

            # This should also work several times.
            a.train()
            wait_for_restore()
            a.train()

            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 2)

            a.stop()

    def test_multi_agent_env_eval_workers_fault_but_restore_env(self):
        # Counter that will survive restarts.
        COUNTER_NAME = "test_multi_agent_env_eval_workers_fault_but_restore_env"
        counter = Counter.options(name=COUNTER_NAME).remote()

        config = (
            PGConfig()
            .rollouts(
                num_rollout_workers=2,
                rollout_fragment_length=16,
            )
            .training(
                train_batch_size=32,
                model={"fcnet_hiddens": [4]},
            )
            .environment(
                env="multi-agent-fault_env",
                # Workers do not fault and no fault tolerance.
                env_config={},
                disable_env_checking=True,
            )
            .multi_agent(
                policies={
                    "main_agent": PolicySpec(),
                },
                policies_to_train=["main_agent"],
                policy_mapping_fn=lambda *args, **kwargs: "main_agent",
            )
            .evaluation(
                evaluation_num_workers=2,
                evaluation_interval=1,
                evaluation_config=PGConfig.overrides(
                    # Now instead of recreating failed workers,
                    # we want to recreate the failed sub env instead.
                    restart_failed_sub_environments=True,
                    env_config={
                        "evaluation": True,
                        "p_terminated": 0.0,
                        "max_episode_len": 20,
                        # Make eval worker (index 1) fail.
                        "bad_indices": [1],
                        "counter": COUNTER_NAME,
                        "failure_start_count": 3,
                        "failure_stop_count": 5,
                    },
                ),
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaciton counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            result = a.train()

            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            self.assertEqual(result["num_faulty_episodes"], 0)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 2)
            # There should be a faulty episode.
            self.assertEqual(result["evaluation"]["num_faulty_episodes"], 2)

            # This should also work several times.
            result = a.train()

            self.assertEqual(a.workers.num_healthy_remote_workers(), 2)
            self.assertEqual(result["num_faulty_episodes"], 0)
            self.assertEqual(a.evaluation_workers.num_healthy_remote_workers(), 2)
            # There shouldn't be any faulty episode anymore.
            self.assertEqual(result["evaluation"]["num_faulty_episodes"], 0)

            a.stop()

    def test_long_failure_period_restore_env(self):
        # Counter that will survive restarts.
        COUNTER_NAME = "test_long_failure_period_restore_env"
        counter = Counter.options(name=COUNTER_NAME).remote()

        config = (
            PGConfig()
            .rollouts(
                num_rollout_workers=1,
                create_env_on_local_worker=False,
            )
            .training(
                model={"fcnet_hiddens": [4]},
            )
            .environment(
                env="fault_env",
                env_config={
                    "restart_failed_sub_environments": True,
                    "p_terminated": 0.0,
                    "max_episode_len": 100,
                    "bad_indices": [1],
                    # Env throws error between steps 30 and 80.
                    "failure_start_count": 30,
                    "failure_stop_count": 80,
                    "counter": COUNTER_NAME,
                },
            )
            .evaluation(
                evaluation_num_workers=1,
                evaluation_interval=1,
                evaluation_config=PGConfig.overrides(
                    env_config={
                        "evaluation": True,
                    }
                ),
            )
            .fault_tolerance(
                # Worker fault tolerance.
                recreate_failed_workers=True,  # Restore failed workers.
                restart_failed_sub_environments=True,  # And create failed envs.
                # 0 delay for testing purposes.
                delay_between_worker_restarts_s=0,
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaciton counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            result = a.train()

            # Should see a lot of faulty episodes.
            self.assertGreaterEqual(result["num_faulty_episodes"], 50)
            self.assertGreaterEqual(result["evaluation"]["num_faulty_episodes"], 50)

            # Rollout and evaluation workers are fine since envs are restored.
            self.assertTrue(a.workers.num_healthy_remote_workers(), 1)
            self.assertTrue(a.evaluation_workers.num_healthy_remote_workers(), 1)

    def test_env_wait_time_workers_restore_env(self):
        # Counter that will survive restarts.
        COUNTER_NAME = "test_env_wait_time_workers_restore_env"
        counter = Counter.options(name=COUNTER_NAME).remote()

        config = (
            PGConfig()
            .rollouts(
                num_rollout_workers=1,
                rollout_fragment_length=5,
                # Use EMA PerfStat.
                # Really large coeff to show the difference in env_wait_time_ms.
                # Pretty much consider the last 2 data points.
                sampler_perf_stats_ema_coef=0.5,
            )
            .training(
                model={"fcnet_hiddens": [4]},
                train_batch_size=10,
            )
            .environment(
                env="fault_env",
                # Workers do not fault and no fault tolerance.
                env_config={
                    "restart_failed_sub_environments": True,
                    "p_terminated": 0.0,
                    "max_episode_len": 10,
                    "init_delay": 10,  # 10 sec init delay.
                    # Make both worker idx=1 and 2 fail.
                    "bad_indices": [1],
                    "failure_start_count": 7,
                    "failure_stop_count": 8,
                    "counter": COUNTER_NAME,
                },
            )
            .reporting(
                # Important, don't smooth over all the episodes,
                # otherwise we don't see latency spike.
                metrics_num_episodes_for_smoothing=1
            )
            .fault_tolerance(
                # Worker fault tolerance.
                recreate_failed_workers=False,  # Do not ignore.
                restart_failed_sub_environments=True,  # But recover.
                # 0 delay for testing purposes.
                delay_between_worker_restarts_s=0,
            )
            .debugging(worker_cls=ForwardHealthCheckToEnvWorker)
        )

        for _ in framework_iterator(config, frameworks=("tf2", "torch")):
            # Reset interaciton counter.
            ray.wait([counter.reset.remote()])

            a = config.build()

            # Had to restore env during this iteration.
            result = a.train()
            self.assertEqual(result["num_faulty_episodes"], 1)
            time_with_restore = result["sampler_perf"]["mean_env_wait_ms"]

            # Doesn't have to restore env during this iteration.
            result = a.train()
            # Still only 1 faulty episode.
            self.assertEqual(result["num_faulty_episodes"], 0)
            time_without_restore = result["sampler_perf"]["mean_env_wait_ms"]

            # wait time with restore is at least 2 times wait time without restore.
            self.assertGreater(time_with_restore, 2 * time_without_restore)

    def test_eval_workers_on_infinite_episodes(self):
        """Tests whether eval workers warn appropriately after some episode timeout."""
        # Create infinitely running episodes, but with horizon setting (RLlib will
        # auto-terminate the episode). However, in the eval workers, don't set a
        # horizon -> Expect warning and no proper evaluation results.
        config = (
            PGConfig()
            .environment(env=RandomEnv, env_config={"p_terminated": 0.0})
            .rollouts(num_rollout_workers=2)
            .reporting(metrics_episode_collection_timeout_s=5.0)
            .evaluation(
                evaluation_num_workers=2,
                evaluation_interval=1,
                evaluation_sample_timeout_s=5.0,
            )
        )
        algo = config.build()
        results = algo.train()
        self.assertTrue(np.isnan(results["evaluation"]["episode_reward_mean"]))


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main(["-v", __file__]))
