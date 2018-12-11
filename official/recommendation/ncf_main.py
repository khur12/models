# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""NCF framework to train and evaluate the NeuMF model.

The NeuMF model assembles both MF and MLP models under the NCF framework. Check
`neumf_model.py` for more details about the models.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import heapq
import math
import multiprocessing
import os
import signal
import typing

# pylint: disable=g-bad-import-order
import numpy as np
from absl import app as absl_app
from absl import flags
import tensorflow as tf
# pylint: enable=g-bad-import-order

from tensorflow.contrib.compiler import xla
from official.datasets import movielens
from official.recommendation import constants as rconst
from official.recommendation import data_pipeline
from official.recommendation import data_preprocessing
from official.recommendation import neumf_model
from official.utils.flags import core as flags_core
from official.utils.logs import hooks_helper
from official.utils.logs import logger
from official.utils.logs import mlperf_helper
from official.utils.misc import distribution_utils
from official.utils.misc import model_helpers


FLAGS = flags.FLAGS


def construct_estimator(model_dir, iterations, params):
  """Construct either an Estimator or TPUEstimator for NCF.

  Args:
    model_dir: The model directory for the estimator
    iterations:  Estimator iterations
    params: The params dict for the estimator

  Returns:
    An Estimator or TPUEstimator.
  """

  if params["use_tpu"]:
    tpu_cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(
        tpu=params["tpu"],
        zone=params["tpu_zone"],
        project=params["tpu_gcp_project"],
        coordinator_name="coordinator"
    )
    tf.logging.info("Issuing reset command to TPU to ensure a clean state.")
    tf.Session.reset(tpu_cluster_resolver.get_master())

    tpu_config = tf.contrib.tpu.TPUConfig(
        iterations_per_loop=iterations,
        num_shards=8)

    run_config = tf.contrib.tpu.RunConfig(
        cluster=tpu_cluster_resolver,
        model_dir=model_dir,
        save_checkpoints_secs=600,
        session_config=tf.ConfigProto(
            allow_soft_placement=True, log_device_placement=False),
        tpu_config=tpu_config)

    tpu_params = {k: v for k, v in params.items() if k != "batch_size"}

    train_estimator = tf.contrib.tpu.TPUEstimator(
        model_fn=neumf_model.neumf_model_fn,
        use_tpu=True,
        train_batch_size=params["batch_size"] * params["batches_per_step"],
        eval_batch_size=params["eval_batch_size"] * params["batches_per_step"],
        params=tpu_params,
        config=run_config)

    eval_estimator = tf.contrib.tpu.TPUEstimator(
        model_fn=neumf_model.neumf_model_fn,
        use_tpu=True,
        train_batch_size=params["batch_size"] * params["batches_per_step"],
        eval_batch_size=params["eval_batch_size"] * params["batches_per_step"],
        params=tpu_params,
        config=run_config)

    return train_estimator, eval_estimator

  distribution = distribution_utils.get_distribution_strategy(num_gpus=params["num_gpus"])
  run_config = tf.estimator.RunConfig(train_distribute=distribution,
                                      eval_distribute=distribution)
  model_fn = neumf_model.neumf_model_fn
  if params["use_xla_for_gpu"]:
    tf.logging.info("Using XLA for GPU for training and evaluation.")
    model_fn = xla.estimator_model_fn(model_fn)
  estimator = tf.estimator.Estimator(model_fn=model_fn, model_dir=model_dir,
                                     config=run_config, params=params)
  return estimator, estimator


def log_and_get_hooks(eval_batch_size):
  # Create hooks that log information about the training and metric values
  train_hooks = hooks_helper.get_train_hooks(
      FLAGS.hooks,
      model_dir=FLAGS.model_dir,
      batch_size=FLAGS.batch_size,  # for ExamplesPerSecondHook
      tensors_to_log={"cross_entropy": "cross_entropy"}
  )
  run_params = {
    "batch_size": FLAGS.batch_size,
    "eval_batch_size": eval_batch_size,
    "number_factors": FLAGS.num_factors,
    "hr_threshold": FLAGS.hr_threshold,
    "train_epochs": FLAGS.train_epochs,
  }
  benchmark_logger = logger.get_benchmark_logger()
  benchmark_logger.log_run_info(
      model_name="recommendation",
      dataset_name=FLAGS.dataset,
      run_params=run_params,
      test_id=FLAGS.benchmark_test_id)

  return benchmark_logger, train_hooks


def parse_flags(flags_obj):
  num_gpus = flags_core.get_num_gpus(flags_obj)

  # TODO(robieta): TPU shards
  num_devices = num_gpus or 1

  batch_size = distribution_utils.per_device_batch_size(
      (int(flags_obj.batch_size) + num_devices - 1) // num_devices * num_devices, num_gpus)

  eval_divisor = (rconst.NUM_EVAL_NEGATIVES + 1) * num_devices
  eval_batch_size = int(flags_obj.eval_batch_size or flags_obj.batch_size or 1)
  eval_batch_size = distribution_utils.per_device_batch_size(
      (eval_batch_size + eval_divisor - 1) // eval_divisor * eval_divisor, num_gpus)

  return {
    "train_epochs": flags_obj.train_epochs,
    "batches_per_step": num_devices,
    "use_seed": flags_obj.seed is not None,
    "hash_pipeline": flags_obj.hash_pipeline,
    "batch_size": batch_size,
    "eval_batch_size": eval_batch_size,
    "learning_rate": flags_obj.learning_rate,
    "mf_dim": flags_obj.num_factors,
    "model_layers": [int(layer) for layer in flags_obj.layers],
    "mf_regularization": flags_obj.mf_regularization,
    "mlp_reg_layers": [float(reg) for reg in flags_obj.mlp_regularization],
    "num_neg": flags_obj.num_neg,
    "num_gpus": num_gpus,
    "use_tpu": flags_obj.tpu is not None,
    "tpu": flags_obj.tpu,
    "tpu_zone": flags_obj.tpu_zone,
    "tpu_gcp_project": flags_obj.tpu_gcp_project,
    "beta1": flags_obj.beta1,
    "beta2": flags_obj.beta2,
    "epsilon": flags_obj.epsilon,
    "match_mlperf": flags_obj.ml_perf,
    "use_xla_for_gpu": flags_obj.use_xla_for_gpu,
  }


def _logitfy(inputs, base_model):
  logits = base_model(inputs)
  zero_tensor = tf.keras.layers.Lambda(lambda x: x * 0)(logits)
  to_concatenate = [zero_tensor, logits]
  concat_layer = tf.keras.layers.Concatenate(axis=1)(to_concatenate)

  reshape_layer = tf.keras.layers.Reshape(
      target_shape=(concat_layer.shape[1].value,))(concat_layer)

  model = tf.keras.Model(inputs=inputs, outputs=reshape_layer)
  return model


def main(_):
  with logger.benchmark_context(FLAGS), \
       mlperf_helper.LOGGER(FLAGS.output_ml_perf_compliance_logging):
    mlperf_helper.set_ncf_root(os.path.split(os.path.abspath(__file__))[0])
    run_ncf(FLAGS)


def run_ncf(_):
  """Run NCF training and eval loop."""
  if FLAGS.download_if_missing and not FLAGS.use_synthetic_data:
    movielens.download(FLAGS.dataset, FLAGS.data_dir)

  if FLAGS.seed is not None:
    np.random.seed(FLAGS.seed)

  params = parse_flags(FLAGS)
  total_training_cycle = FLAGS.train_epochs // FLAGS.epochs_between_evals

  if FLAGS.use_synthetic_data:
    producer = data_pipeline.DummyConstructor()
    num_users, num_items = data_preprocessing.DATASET_TO_NUM_USERS_AND_ITEMS[
        FLAGS.dataset]
    num_train_steps = rconst.SYNTHETIC_BATCHES_PER_EPOCH
    num_eval_steps = rconst.SYNTHETIC_BATCHES_PER_EPOCH
  else:
    ncf_dataset, producer = data_preprocessing.instantiate_pipeline(
        dataset=FLAGS.dataset, data_dir=FLAGS.data_dir, num_data_readers=None,
        match_mlperf=FLAGS.ml_perf, deterministic=FLAGS.seed is not None,
        params=params)

    num_users = ncf_dataset.num_users
    num_items = ncf_dataset.num_items
    num_train_steps = (producer.train_batches_per_epoch //
                       params["batches_per_step"])
    num_eval_steps = (producer.eval_batches_per_epoch //
                      params["batches_per_step"])
    assert not producer.train_batches_per_epoch % params["batches_per_step"]
    assert not producer.eval_batches_per_epoch % params["batches_per_step"]
  producer.start()

  params["num_users"], params["num_items"] = num_users, num_items
  model_helpers.apply_clean(flags.FLAGS)

  target_reached = False
  mlperf_helper.ncf_print(key=mlperf_helper.TAGS.TRAIN_LOOP)

  if FLAGS.use_keras:
    print(">>>>>> zhenzheng use_keras")

    train_input_fn = data_preprocessing.make_input_fn(
        producer, is_training=True, use_tpu=False)

    user_input = tf.keras.layers.Input(
        shape=(1,), batch_size=FLAGS.batch_size, name="user_id", dtype=tf.int32)
    item_input = tf.keras.layers.Input(
        shape=(1,), batch_size=FLAGS.batch_size, name="item_id", dtype=tf.int32)

    base_model = neumf_model.construct_model_keras(user_input, item_input, params)
    keras_model = _logitfy([user_input, item_input], base_model)

    keras_model.summary()

    def softmax_crossentropy_with_logits(y_true, y_pred):
      """A loss function replicating tf's sparse_softmax_cross_entropy
      Args:
        y_true: True labels. Tensor of shape [batch_size,]
        y_pred: Predictions. Tensor of shape [batch_size, num_classes]
      """
      y_true = tf.cast(y_true, tf.int32)
      return tf.losses.sparse_softmax_cross_entropy(
        labels=tf.reshape(y_true, [FLAGS.batch_size,]),
        logits=tf.reshape(y_pred, [FLAGS.batch_size, 2]))

    opt = neumf_model.get_optimizer(params)
    strategy = distribution_utils.get_distribution_strategy(num_gpus=1)

    keras_model.compile(loss=softmax_crossentropy_with_logits,
        optimizer=opt,
        metrics=['accuracy'],
        distribute=None)

    num_train_steps = (producer.train_batches_per_epoch //
        params["batches_per_step"])

    train_input_dataset = train_input_fn(params).repeat(FLAGS.train_epochs)

    keras_model.fit(train_input_dataset,
        epochs=FLAGS.train_epochs,
        steps_per_epoch=num_train_steps,
        callbacks=[],
        verbose=0)

    return


  # Not use Keras
  train_estimator, eval_estimator = construct_estimator(
      model_dir=FLAGS.model_dir, iterations=num_train_steps, params=params)

  benchmark_logger, train_hooks = log_and_get_hooks(params["eval_batch_size"])

  for cycle_index in range(total_training_cycle):
    assert FLAGS.epochs_between_evals == 1 or not mlperf_helper.LOGGER.enabled
    tf.logging.info("Starting a training cycle: {}/{}".format(
        cycle_index + 1, total_training_cycle))

    mlperf_helper.ncf_print(key=mlperf_helper.TAGS.TRAIN_EPOCH,
                            value=cycle_index)

    train_input_fn = data_preprocessing.make_input_fn(
                producer=producer, is_training=True, use_tpu=params["use_tpu"])
    train_estimator.train(input_fn=train_input_fn, hooks=train_hooks,
                          steps=num_train_steps)

    tf.logging.info("Beginning evaluation.")
    eval_input_fn = data_preprocessing.make_input_fn(
                producer=producer, is_training=False, use_tpu=params["use_tpu"])

    mlperf_helper.ncf_print(key=mlperf_helper.TAGS.EVAL_START,
                            value=cycle_index)
    eval_results = eval_estimator.evaluate(eval_input_fn,
                                           steps=num_eval_steps)
    tf.logging.info("Evaluation complete.")

    hr = float(eval_results[rconst.HR_KEY])
    ndcg = float(eval_results[rconst.NDCG_KEY])
    loss = float(eval_results["loss"])

    mlperf_helper.ncf_print(
        key=mlperf_helper.TAGS.EVAL_TARGET,
        value={"epoch": cycle_index, "value": FLAGS.hr_threshold})
    mlperf_helper.ncf_print(key=mlperf_helper.TAGS.EVAL_ACCURACY,
                            value={"epoch": cycle_index, "value": hr})
    mlperf_helper.ncf_print(
        key=mlperf_helper.TAGS.EVAL_HP_NUM_NEG,
        value={"epoch": cycle_index, "value": rconst.NUM_EVAL_NEGATIVES})

    mlperf_helper.ncf_print(key=mlperf_helper.TAGS.EVAL_STOP, value=cycle_index)

    # Benchmark the evaluation results
    benchmark_logger.log_evaluation_result(eval_results)
    # Log the HR and NDCG results.
    tf.logging.info(
        "Iteration {}: HR = {:.4f}, NDCG = {:.4f}, Loss = {:.4f}".format(
            cycle_index + 1, hr, ndcg, loss))

    # If some evaluation threshold is met
    if model_helpers.past_stop_threshold(FLAGS.hr_threshold, hr):
      target_reached = True
      break

  mlperf_helper.ncf_print(key=mlperf_helper.TAGS.RUN_STOP,
                          value={"success": target_reached})
  producer.stop_loop()
  producer.join()

  # Clear the session explicitly to avoid session delete error
  tf.keras.backend.clear_session()

  mlperf_helper.ncf_print(key=mlperf_helper.TAGS.RUN_FINAL)


def define_ncf_flags():
  """Add flags for running ncf_main."""
  # Add common flags
  flags_core.define_base(export_dir=False)
  flags_core.define_performance(
      num_parallel_calls=False,
      inter_op=False,
      intra_op=False,
      synthetic_data=True,
      max_train_steps=False,
      dtype=False,
      all_reduce_alg=False
  )
  flags_core.define_device(tpu=True)
  flags_core.define_benchmark()

  flags.adopt_module_key_flags(flags_core)

  flags_core.set_defaults(
      model_dir="/tmp/ncf/",
      data_dir="/tmp/movielens-data/",
      train_epochs=2,
      batch_size=256,
      hooks="ProfilerHook",
      tpu=None
  )

  # Add ncf-specific flags
  flags.DEFINE_enum(
      name="dataset", default="ml-1m",
      enum_values=["ml-1m", "ml-20m"], case_sensitive=False,
      help=flags_core.help_wrap(
          "Dataset to be trained and evaluated."))

  flags.DEFINE_boolean(
      name="download_if_missing", default=True, help=flags_core.help_wrap(
          "Download data to data_dir if it is not already present."))

  flags.DEFINE_string(
      name="eval_batch_size", default=None, help=flags_core.help_wrap(
          "The batch size used for evaluation. This should generally be larger"
          "than the training batch size as the lack of back propagation during"
          "evaluation can allow for larger batch sizes to fit in memory. If not"
          "specified, the training batch size (--batch_size) will be used."))

  flags.DEFINE_integer(
      name="num_factors", default=8,
      help=flags_core.help_wrap("The Embedding size of MF model."))

  # Set the default as a list of strings to be consistent with input arguments
  flags.DEFINE_list(
      name="layers", default=["64", "32", "16", "8"],
      help=flags_core.help_wrap(
          "The sizes of hidden layers for MLP. Example "
          "to specify different sizes of MLP layers: --layers=32,16,8,4"))

  flags.DEFINE_float(
      name="mf_regularization", default=0.,
      help=flags_core.help_wrap(
          "The regularization factor for MF embeddings. The factor is used by "
          "regularizer which allows to apply penalties on layer parameters or "
          "layer activity during optimization."))

  flags.DEFINE_list(
      name="mlp_regularization", default=["0.", "0.", "0.", "0."],
      help=flags_core.help_wrap(
          "The regularization factor for each MLP layer. See mf_regularization "
          "help for more info about regularization factor."))

  flags.DEFINE_integer(
      name="num_neg", default=4,
      help=flags_core.help_wrap(
          "The Number of negative instances to pair with a positive instance."))

  flags.DEFINE_float(
      name="learning_rate", default=0.001,
      help=flags_core.help_wrap("The learning rate."))

  flags.DEFINE_float(
      name="beta1", default=0.9,
      help=flags_core.help_wrap("beta1 hyperparameter for the Adam optimizer."))

  flags.DEFINE_float(
      name="beta2", default=0.999,
      help=flags_core.help_wrap("beta2 hyperparameter for the Adam optimizer."))

  flags.DEFINE_float(
      name="epsilon", default=1e-8,
      help=flags_core.help_wrap("epsilon hyperparameter for the Adam "
                                "optimizer."))

  flags.DEFINE_float(
      name="hr_threshold", default=None,
      help=flags_core.help_wrap(
          "If passed, training will stop when the evaluation metric HR is "
          "greater than or equal to hr_threshold. For dataset ml-1m, the "
          "desired hr_threshold is 0.68 which is the result from the paper; "
          "For dataset ml-20m, the threshold can be set as 0.95 which is "
          "achieved by MLPerf implementation."))

  flags.DEFINE_bool(
      name="ml_perf", default=False,
      help=flags_core.help_wrap(
          "If set, changes the behavior of the model slightly to match the "
          "MLPerf reference implementations here: \n"
          "https://github.com/mlperf/reference/tree/master/recommendation/"
          "pytorch\n"
          "The two changes are:\n"
          "1. When computing the HR and NDCG during evaluation, remove "
          "duplicate user-item pairs before the computation. This results in "
          "better HRs and NDCGs.\n"
          "2. Use a different soring algorithm when sorting the input data, "
          "which performs better due to the fact the sorting algorithms are "
          "not stable."))

  flags.DEFINE_bool(
      name="output_ml_perf_compliance_logging", default=False,
      help=flags_core.help_wrap(
          "If set, output the MLPerf compliance logging. This is only useful "
          "if one is running the model for MLPerf. See "
          "https://github.com/mlperf/policies/blob/master/training_rules.adoc"
          "#submission-compliance-logs for details. This uses sudo and so may "
          "ask for your password, as root access is needed to clear the system "
          "caches, which is required for MLPerf compliance."
      )
  )

  flags.DEFINE_integer(
      name="seed", default=None, help=flags_core.help_wrap(
          "This value will be used to seed both NumPy and TensorFlow."))

  flags.DEFINE_bool(
      name="hash_pipeline", default=False, help=flags_core.help_wrap(
          "This flag will perform a separate run of the pipeline and hash "
          "batches as they are produced. \nNOTE: this will significantly slow "
          "training. However it is useful to confirm that a random seed is "
          "does indeed make the data pipeline deterministic."))

  @flags.validator("eval_batch_size", "eval_batch_size must be at least {}"
                   .format(rconst.NUM_EVAL_NEGATIVES + 1))
  def eval_size_check(eval_batch_size):
    return (eval_batch_size is None or
            int(eval_batch_size) > rconst.NUM_EVAL_NEGATIVES)

  flags.DEFINE_bool(
      name="use_subprocess", default=True, help=flags_core.help_wrap(
          "By default, ncf_main.py starts async data generation process as a "
          "subprocess. If set to False, ncf_main.py will assume the async data "
          "generation process has already been started by the user."))

  flags.DEFINE_integer(name="cache_id", default=None, help=flags_core.help_wrap(
      "Use a specified cache_id rather than using a timestamp. This is only "
      "needed to synchronize across multiple workers. Generally this flag will "
      "not need to be set."
  ))

  flags.DEFINE_bool(
      name="use_xla_for_gpu", default=False, help=flags_core.help_wrap(
          "If True, use XLA for the model function. Only works when using a "
          "GPU. On TPUs, XLA is always used"))

  flags.DEFINE_bool(
      name="use_keras", default=False, help=flags_core.help_wrap(
          "If True, use XLA for the model function. Only works when using a "
          "GPU. On TPUs, XLA is always used"))

  xla_message = "--use_xla_for_gpu is incompatible with --tpu"
  @flags.multi_flags_validator(["use_xla_for_gpu", "tpu"], message=xla_message)
  def xla_validator(flag_dict):
    return not flag_dict["use_xla_for_gpu"] or not flag_dict["tpu"]


if __name__ == "__main__":
  tf.logging.set_verbosity(tf.logging.INFO)
  define_ncf_flags()
  absl_app.run(main)
