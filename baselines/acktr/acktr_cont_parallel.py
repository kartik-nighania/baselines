import numpy as np
import tensorflow as tf
from baselines import logger
from baselines import common
from baselines.common import tf_util as U
from baselines.acktr import kfac
from baselines.acktr.filters import ZFilter
import os
import pickle


def pathlength(path):
    return path["reward"].shape[0]# Loss function that we'll differentiate to get the policy gradient

def rollout(env, policy, max_pathlength, animate=False, obfilter=None):
    """
    Simulate the env and policy for max_pathlength steps
    """
    ob = env.reset()
    prev_ob = np.float32(np.zeros(ob.shape))
    if obfilter: ob = obfilter(ob)
    terminated = False

    obs = []
    acs = []
    ac_dists = []
    logps = []
    rewards = []
    for _ in range(max_pathlength):
        if animate:
            env.render()
        state = np.concatenate([ob, prev_ob], -1)
        obs.append(state)
        ac, ac_dist, logp = policy.act(state)
        acs.append(ac)
        ac_dists.append(ac_dist)
        logps.append(logp)
        prev_ob = np.copy(ob)
        scaled_ac = env.action_space.low + (ac + 1.) * 0.5 * (env.action_space.high - env.action_space.low)
        scaled_ac = np.clip(scaled_ac, env.action_space.low, env.action_space.high)
        ob, rew, done, _ = env.step(scaled_ac)
        if obfilter: ob = obfilter(ob)
        rewards.append(rew)
        if done:
            terminated = True
            break
    return {"observation" : np.array(obs), "terminated" : terminated,
            "reward" : np.array(rewards), "action" : np.array(acs),
            "action_dist": np.array(ac_dists), "logp" : np.array(logps)}


def rollout_parallel(env, policy, max_pathlength, animate=False, obfilter=None):
    """
    Simulate the env and policy for max_pathlength steps
    """
  #  ob = env.reset()
    ob = env.reset_parallel()
    prev_ob = np.float32(np.zeros(ob.shape))
    if obfilter: ob = obfilter(ob)
    terminated = False

    obs = []
    acs = []
    ac_dists = []
    logps = []
    rewards = []

    mb_obs, mb_rewards, mb_actions, mb_values, mb_dones = [],[],[],[],[]
    for _ in range(max_pathlength):
        if animate:
            env.render()
        state = np.concatenate([ob, prev_ob], -1)
        obs.append(state)
        ac, ac_dist, logp = policy.acact_parallelt(state) # add ?
        acs.append(ac)
        ac_dists.append(ac_dist)
        logps.append(logp)
        prev_ob = np.copy(ob)
        scaled_ac = env.action_space.low + (ac + 1.) * 0.5 * (env.action_space.high - env.action_space.low)
        scaled_ac = np.clip(scaled_ac, env.action_space.low, env.action_space.high)
        
        ob, rew, done, _ = env.step_parallel(scaled_ac)
        #batch of steps to batch of rollouts
    #    mb_obs = np.asarray(mb_obs, dtype=np.float32).swapaxes(1, 0).reshape(self.batch_ob_shape)
    #    mb_rewards = np.asarray(mb_rewards, dtype=np.float32).swapaxes(1, 0)
    #    mb_actions = np.asarray(mb_actions, dtype=np.int32).swapaxes(1, 0)
    #    mb_values = np.asarray(mb_values, dtype=np.float32).swapaxes(1, 0)
    #    mb_dones = np.asarray(mb_dones, dtype=np.bool).swapaxes(1, 0)
    #    mb_masks = mb_dones[:, :-1]
    #    mb_dones = mb_dones[:, 1:]

        if obfilter: ob = obfilter(ob)
        rewards.append(rew)
        if done:
            terminated = True
            break
    return {"observation" : np.array(obs), "terminated" : terminated,
            "reward" : np.array(rewards), "action" : np.array(acs),
            "action_dist": np.array(ac_dists), "logp" : np.array(logps)}


def learn(env, policy, vf, gamma, lam, timesteps_per_batch, resume, agentName, logdir, num_timesteps,
    animate=False, num_parallel=1, callback=None, desired_kl=0.002, max_grad_norm=1.0, save_interval=10, 
    lrschedule='linear'):

    obfilter = ZFilter(env.observation_space.shape)

    max_pathlength = env.spec.timestep_limit
    stepsize = tf.Variable(initial_value=np.float32(np.array(0.03)), name='stepsize')
    inputs, loss, loss_sampled = policy.update_info
    optim = kfac.KfacOptimizer(learning_rate=stepsize, cold_lr=stepsize*(1.0 - 0.9), momentum=0.9, kfac_update=2,\
                                epsilon=1e-2, stats_decay=0.99, async=1, cold_iter=1,
                                weight_decay_dict=policy.wd_dict, max_grad_norm=max_grad_norm)
    pi_var_list = []
    for var in tf.trainable_variables():
        if "pi" in var.name:
            pi_var_list.append(var)

    update_op, q_runner = optim.minimize(loss, loss_sampled, var_list=pi_var_list)
    do_update = U.function(inputs, update_op)
    U.initialize()

    # start queue runners
    enqueue_threads = []
    coord = tf.train.Coordinator()
    for qr in [q_runner, vf.q_runner]:
        assert (qr != None)
        enqueue_threads.extend(qr.create_threads(U.get_session(), coord=coord, start=True))

    timesteps_so_far = 0
    saver = tf.train.Saver(max_to_keep = 10)
    if resume > 0:
        saver.restore(tf.get_default_session(), os.path.join(os.path.abspath(logdir), "{}-{}".format(agentName, resume)))
        ob_filter_path = os.path.join(os.path.abspath(logdir), "{}-{}".format('obfilter', resume))
        with open(ob_filter_path, 'rb') as ob_filter_input:
            obfilter = pickle.load(ob_filter_input)
    iters_so_far = resume

    logF = open(os.path.join(logdir, 'log.txt'), 'a')
    logF2 = open(os.path.join(logdir, 'log_it.txt'), 'a')
    logStats = open(os.path.join(logdir, 'log_stats.txt'), 'a')

    while True:
        if timesteps_so_far > num_timesteps:
            break
        logger.log("********** Iteration %i ************"%iters_so_far)

        # Collect paths until we have enough timesteps
        timesteps_this_batch = 0
        paths = []
        while True:
#            path = rollout(env, policy, max_pathlength, animate=(len(paths)==0 and (i % 10 == 0) and animate), obfilter=obfilter)
            path = rollout(env, policy, max_pathlength, animate=(len(paths)==0 and (iters_so_far % 10 == 0) and animate), obfilter=obfilter)
            paths.append(path)
            n = pathlength(path)
            timesteps_this_batch += n
            timesteps_so_far += n
            if timesteps_this_batch > timesteps_per_batch:
                break

        # Estimate advantage function
        vtargs = []
        advs = []
        for path in paths:
            rew_t = path["reward"]
            return_t = common.discount(rew_t, gamma)
            vtargs.append(return_t)
            vpred_t = vf.predict(path)
            vpred_t = np.append(vpred_t, 0.0 if path["terminated"] else vpred_t[-1])
            delta_t = rew_t + gamma*vpred_t[1:] - vpred_t[:-1]
            adv_t = common.discount(delta_t, gamma * lam)
            advs.append(adv_t)
        # Update value function
        vf.fit(paths, vtargs)

        # Build arrays for policy update
        ob_no = np.concatenate([path["observation"] for path in paths])
        action_na = np.concatenate([path["action"] for path in paths])
        oldac_dist = np.concatenate([path["action_dist"] for path in paths])
        logp_n = np.concatenate([path["logp"] for path in paths])
        adv_n = np.concatenate(advs)
        standardized_adv_n = (adv_n - adv_n.mean()) / (adv_n.std() + 1e-8)

        # Policy update
        do_update(ob_no, action_na, standardized_adv_n)

        min_stepsize = np.float32(1e-8)
        max_stepsize = np.float32(1e0)

        # Adjust stepsize
        kl = policy.compute_kl(ob_no, oldac_dist)
        if kl > desired_kl * 2.0:
            logger.log("kl too high")
            U.eval(tf.assign(stepsize, tf.maximum(min_stepsize, stepsize / 1.5)))
        elif kl < desired_kl / 2.0:
            logger.log("kl too low")
            U.eval(tf.assign(stepsize, tf.minimum(max_stepsize, stepsize * 1.5)))
        else:
            logger.log("kl just right!")

        rew_mean = np.mean([path["reward"].sum() for path in paths])
        logger.record_tabular("EpRewMean", rew_mean)
        logger.record_tabular("EpRewSEM", np.std([path["reward"].sum()/np.sqrt(len(paths)) for path in paths]))
        logger.record_tabular("EpLenMean", np.mean([pathlength(path) for path in paths]))
        logger.record_tabular("KL", kl)

        logF.write(str(rew_mean) + "\n")
        logF2.write(str(iters_so_far) + "," + str(rew_mean) + "\n")
     #   json.dump(combined_stats, logStats)
        logF.flush()
        logF2.flush()
     #   logStats.flush()

        if save_interval and (iters_so_far % save_interval == 0 or iters_so_far == 1):
            saver.save(tf.get_default_session(), os.path.join(logdir, agentName), global_step=iters_so_far)
            ob_filter_path = os.path.join(os.path.abspath(logdir), "{}-{}".format('obfilter', iters_so_far))
            with open(ob_filter_path, 'wb') as ob_filter_output:
                pickle.dump(obfilter, ob_filter_output, pickle.HIGHEST_PROTOCOL)

        if callback:
            callback()
        logger.dump_tabular()
        iters_so_far += 1


