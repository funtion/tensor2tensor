# coding=utf-8
# Copyright 2019 The Tensor2Tensor Authors.
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

r"""PPO binary over a gym env.

Sample invocation:

ENV_PROBLEM_NAME=Acrobot-v1
COMBINED_NETWORK=false
EPOCHS=100
BATCH_SIZE=32
RANDOM_SEED=0
BOUNDARY=100

python trax/rlax/ppo_main.py \
  --env_problem_name=${ENV_PROBLEM_NAME} \
  --combined_policy_and_value_function=${COMBINED_NETWORK} \
  --epochs=${EPOCHS} \
  --batch_size=${BATCH_SIZE} \
  --random_seed=${RANDOM_SEED} \
  --boundary=${BOUNDARY} \
  --vmodule=*/tensor2tensor/*=1 \
  --alsologtostderr \
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools

from absl import app
from absl import flags
import gym
import jax
from jax.config import config
import numpy as onp
from tensor2tensor.envs import env_problem
from tensor2tensor.envs import rendered_env_problem
from tensor2tensor.rl import gym_utils
from tensor2tensor.trax import layers
from tensor2tensor.trax.models import atari_cnn
from tensor2tensor.trax.rlax import ppo

FLAGS = flags.FLAGS

flags.DEFINE_string("env_name", None, "Name of the environment to make.")
flags.DEFINE_string("env_problem_name", None, "Name of the EnvProblem to make.")

flags.DEFINE_integer("epochs", 100, "Number of epochs to run for.")
flags.DEFINE_string("random_seed", None, "Random seed.")
flags.DEFINE_integer("batch_size", 32, "Batch of trajectories needed.")

flags.DEFINE_integer(
    "boundary", 20, "We pad trajectories at integer multiples of this number.")
# -1: returns env as is.
# None: unwraps and returns without TimeLimit wrapper.
# Any other number: imposes this restriction.
flags.DEFINE_integer(
    "max_timestep", None,
    "If set to an integer, maximum number of time-steps in a "
    "trajectory. The bare env is wrapped with TimeLimit wrapper.")

# This is different from max_timestep is that in the above, the env is wrapped
# in a TimeLimit wrapper, vs here we use this in the collect function.
flags.DEFINE_integer(
    "truncation_timestep", None,
    "If set to an integer, maximum number of time-steps in a "
    "trajectory. Used in the collect procedure.")

flags.DEFINE_boolean(
    "jax_debug_nans", False,
    "Setting to true will help to debug nans and disable jit.")
flags.DEFINE_boolean("disable_jit", False, "Setting to true will disable jit.")

# If resize is True, then we create RenderedEnvProblem, so this has to be set to
# False for something like CartPole.
flags.DEFINE_boolean("resize", False, "If true, resize the game frame")
flags.DEFINE_integer("resized_height", 105, "Resized height of the game frame.")
flags.DEFINE_integer("resized_width", 80, "Resized width of the game frame.")

flags.DEFINE_boolean(
    "combined_network", False,
    "If True there is a single network that determines policy"
    "and values.")

flags.DEFINE_bool(
    "two_towers", True,
    "In the combined network case should we make one tower or"
    "two.")

flags.DEFINE_boolean("flatten_dims", False,
                     "If true, we flatten except the first two dimensions.")

# Number of optimizer steps of the combined net, policy net and value net.
flags.DEFINE_integer("num_optimizer_steps", 100, "Number of optimizer steps.")
flags.DEFINE_integer("policy_only_num_optimizer_steps", 80,
                     "Number of optimizer steps policy only.")
flags.DEFINE_integer("value_only_num_optimizer_steps", 80,
                     "Number of optimizer steps value only.")
flags.DEFINE_integer(
    "print_every_optimizer_steps", 1,
    "How often to log during the policy optimization process.")

# Learning rate of the combined net, policy net and value net.
flags.DEFINE_float("learning_rate", 1e-3, "Learning rate.")
flags.DEFINE_float("policy_only_learning_rate", 3e-4,
                   "Learning rate for policy network only.")
flags.DEFINE_float("value_only_learning_rate", 1e-3,
                   "Learning rate for value network only.")

# Target KL is used for doing early stopping in the
flags.DEFINE_float("target_kl", 0.01, "Policy iteration early stopping")
flags.DEFINE_float("value_coef", 1.0,
                   "Coefficient of Value Loss term in combined loss.")
flags.DEFINE_float("entropy_coef", 0.01,
                   "Coefficient of the Entropy Bonus term in combined loss.")
flags.DEFINE_float("gamma", 0.99, "Policy iteration early stopping")
flags.DEFINE_float("lambda_", 0.95, "Policy iteration early stopping")
flags.DEFINE_float("epsilon", 0.1, "Policy iteration early stopping")

flags.DEFINE_string("output_dir", "", "Output dir.")
flags.DEFINE_bool("use_tpu", False, "Whether we're running on TPU.")
flags.DEFINE_bool("enable_early_stopping", True,
                  "Whether to enable early stopping.")
flags.DEFINE_bool("xm", False, "Are we running on borg?.")
flags.DEFINE_integer("eval_every_n", 100, "How frequently to eval the policy.")
flags.DEFINE_integer("eval_batch_size", 4, "Batch size for evaluation.")


def common_layers():
  # TODO(afrozm): Refactor.
  if "Pong" in FLAGS.env_problem_name:
    return atari_layers()

  cur_layers = []
  if FLAGS.flatten_dims:
    cur_layers = [layers.Div(divisor=255.0), layers.Flatten(num_axis_to_keep=2)]
  body = [layers.Dense(64), layers.Tanh(), layers.Dense(64), layers.Tanh()]
  return cur_layers + body


def atari_layers():
  return [atari_cnn.AtariCnn()]


def make_env(batch_size=8):
  """Creates the env."""
  if FLAGS.env_name:
    return gym.make(FLAGS.env_name)

  assert FLAGS.env_problem_name

  # No resizing needed, so let's be on the normal EnvProblem.
  if not FLAGS.resize:  # None or False
    return env_problem.EnvProblem(
        base_env_name=FLAGS.env_problem_name,
        batch_size=batch_size,
        reward_range=(-1, 1))

  wrapper_fn = functools.partial(
      gym_utils.gym_env_wrapper, **{
          "rl_env_max_episode_steps": FLAGS.max_timestep,
          "maxskip_env": True,
          "rendered_env": True,
          "rendered_env_resize_to": (FLAGS.resized_height, FLAGS.resized_width),
          "sticky_actions": False,
          "output_dtype": onp.int32 if FLAGS.use_tpu else None,
      })

  return rendered_env_problem.RenderedEnvProblem(
      base_env_name=FLAGS.env_problem_name,
      batch_size=batch_size,
      env_wrapper_fn=wrapper_fn,
      reward_range=(-1, 1))


def get_optimizer_fun(learning_rate):
  return functools.partial(ppo.optimizer_fun, step_size=learning_rate)


def main(argv):
  del argv

  if FLAGS.jax_debug_nans:
    config.update("jax_debug_nans", True)
  if FLAGS.use_tpu:
    config.update("jax_platform_name", "tpu")

  # TODO(afrozm): Refactor.
  if "Pong" in FLAGS.env_problem_name and FLAGS.xm:
    from tensor2tensor.rl.google import atari_utils  # pylint: disable=g-import-not-at-top
    FLAGS.atari_roms_path = "local_ram_fs_tmp"
    atari_utils.copy_roms()

  # Make an env here.
  env = make_env(batch_size=FLAGS.batch_size)
  assert env

  eval_env = make_env(batch_size=FLAGS.eval_batch_size)
  assert eval_env

  def run_training_loop():
    """Runs the training loop."""
    policy_net_fun = None
    value_net_fun = None
    policy_and_value_net_fun = None
    policy_optimizer_fun = None
    value_optimizer_fun = None
    policy_and_value_optimizer_fun = None

    if FLAGS.combined_network:
      policy_and_value_net_fun = functools.partial(
          ppo.policy_and_value_net,
          bottom_layers_fn=common_layers,
          two_towers=FLAGS.two_towers)
      policy_and_value_optimizer_fun = get_optimizer_fun(FLAGS.learning_rate)
    else:
      policy_net_fun = functools.partial(
          ppo.policy_net, bottom_layers=common_layers())
      value_net_fun = functools.partial(
          ppo.value_net, bottom_layers=common_layers())
      policy_optimizer_fun = get_optimizer_fun(FLAGS.policy_only_learning_rate)
      value_optimizer_fun = get_optimizer_fun(FLAGS.value_only_learning_rate)

    random_seed = None
    try:
      random_seed = int(FLAGS.random_seed)
    except Exception:  # pylint: disable=broad-except
      pass

    ppo.training_loop(
        env=env,
        epochs=FLAGS.epochs,
        policy_net_fun=policy_net_fun,
        value_net_fun=value_net_fun,
        policy_and_value_net_fun=policy_and_value_net_fun,
        policy_optimizer_fun=policy_optimizer_fun,
        value_optimizer_fun=value_optimizer_fun,
        policy_and_value_optimizer_fun=policy_and_value_optimizer_fun,
        num_optimizer_steps=FLAGS.num_optimizer_steps,
        policy_only_num_optimizer_steps=FLAGS.policy_only_num_optimizer_steps,
        value_only_num_optimizer_steps=FLAGS.value_only_num_optimizer_steps,
        print_every_optimizer_steps=FLAGS.print_every_optimizer_steps,
        batch_size=FLAGS.batch_size,
        target_kl=FLAGS.target_kl,
        boundary=FLAGS.boundary,
        max_timestep=FLAGS.truncation_timestep,
        random_seed=random_seed,
        c1=FLAGS.value_coef,
        c2=FLAGS.entropy_coef,
        gamma=FLAGS.gamma,
        lambda_=FLAGS.lambda_,
        epsilon=FLAGS.epsilon,
        enable_early_stopping=FLAGS.enable_early_stopping,
        output_dir=FLAGS.output_dir,
        eval_every_n=FLAGS.eval_every_n,
        eval_env=eval_env)

  if FLAGS.jax_debug_nans or FLAGS.disable_jit:
    with jax.disable_jit():
      run_training_loop()
  else:
    run_training_loop()


if __name__ == "__main__":
  app.run(main)
