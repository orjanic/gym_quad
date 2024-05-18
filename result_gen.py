import numpy as np
import matplotlib.pyplot as plt
import gymnasium as gym
import gym_quad
import os
import glob
import re
import pandas as pd

from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.patches import Rectangle
from PIL import Image
from RTvisualizer import *
from pv_plotting_3d import Plotter3D, Plotter3DMultiTraj

from mpl_toolkits.mplot3d import Axes3D
from stable_baselines3 import PPO
from train_run_utils import *
from argparse import Namespace
from gym_quad import register_lv_vae_envs
from drl_config import lv_vae_config

'''
REMEMBER TO 2X CHECK THE VAE CONFIG BEFORE STARTING RESULTS GENERATION
This script can be run from cli. Four modes of operation are supported:

1. Testing all trained agents:
    1.1. Sequentially in one terminal (slower)
        Example:
            python result_gen.py --exp_id 19 --episodes 10 --trained_list expert expert_perturbed --test_list horizontal vertical deadend random_corridor --test_all_agents True
    1.2. Parallell in multiple terminals (faster)
        Example:
            terminal 1:
                python result_gen.py --exp_id 19 --episodes 10 --trained_list expert --test_list horizontal --test_all_agents True
            terminal 2:
                python result_gen.py --exp_id 19 --episodes 10 --trained_list expert --test_list vertical --test_all_agents True
            terminal 3:
                python result_gen.py --exp_id 19 --episodes 10 --trained_list expert --test_list deadend --test_all_agents True
            .
            .
            .

2. Testing a specific trained agent
    2.1. Sequentially in one terminal (slower)
        Example:
            python result_gen.py --exp_id 19 --episodes 10 --trained_list expert --test_list house --agent 170000
    2.2. Parallell in multiple terminals (faster)
        Example:
            terminal 1:
                python result_gen.py --exp_id 19 --episodes 10 --trained_list expert --test_list house --agent 170000
            terminal 2:
                python result_gen.py --exp_id 19 --episodes 10 --trained_list expert --test_list horizontal --agent 170000
            terminal 3:
                python result_gen.py --exp_id 19 --episodes 10 --trained_list expert --test_list vertical --agent 170000
            .
            .
            .

'''

# Set up the env config for testing:
result_config = lv_vae_config.copy()
result_config["max_t_steps"] = 6000
result_config["recap_chance"] = 0
result_config["perturb_sim"] = True

if __name__ == "__main__":

    #TODO as the house and the other scenarios require different LV_VAE_config param we need to figure out
    #To switch config between house and the other scenarios. Can do manually, but auto would be nice.
    
    #---# For debugging and manual running #---#
    # test_scenarios = ["horizontal","vertical","deadend","random_corridor"]
    # trained_scenarios_to_run = ["expert","proficient_perturbed","expert_perturbed"]    
    # args = Namespace(env="LV_VAE_MESH-v0", episodes=10, save_depth_maps=False, exp_id=19) 
    # base_experiment_dir = os.path.join(r"./log", r"{}".format(args.env), r"Experiment {}".format(args.exp_id))
    #---# For debugging and manual running #---#

    #---# When running from cli #---#
    _, _, args = parse_experiment_info()
    test_scenarios = args.test_list
    trained_scenarios_to_run = args.trained_list
    base_experiment_dir = os.path.join(r"./log", r"{}".format(args.env), r"Experiment {}".format(args.exp_id))
    #---# When running from cli #---#

    # print("BASE EXP DIR", base_experiment_dir)
    for trained_scen in trained_scenarios_to_run:
        agent_path = None
        if args.test_all_agents:
            agent_path = os.path.join(base_experiment_dir, trained_scen)
            available_agents = glob.glob(os.path.join(agent_path, "agents", "*.zip"))
        else:
            agent_path = os.path.join(base_experiment_dir, trained_scen)
            available_agents = [f"{base_experiment_dir}/{trained_scen}/agents/model_{args.agent}.zip" if args.agent != None else f"{base_experiment_dir}/{trained_scen}/agents/last_model.zip"]
        
        for agent in available_agents:
            agent_name = os.path.splitext(os.path.basename(agent))[0]
            for test_scen in test_scenarios:
                if test_scen == "house":
                    result_config["la_dist"] = 0.5
                    result_config["s_max"] = 1
                else:
                    result_config["la_dist"] = lv_vae_config["la_dist"]
                    result_config["s_max"] = lv_vae_config["s_max"]
                
                register_lv_vae_envs(result_config)

                # Construct the directory paths
                results_gen_dir = os.path.join(agent_path, "results_gen")
                test_scen_dir = os.path.join(results_gen_dir, test_scen)
                os.makedirs(test_scen_dir, exist_ok=True)

                # Generate the test directory name based on agent name
                test_dir = os.path.join(test_scen_dir, f"test_agent_{agent_name}")
                if os.path.exists(test_dir):
                    #If the directory already exists skip to the next scenario
                    print(f"Test directory for agent {agent_name} in scenario {test_scen} already exists. Skipping...")
                    continue

                    #IF you want this functionality instead, remove the continue and else. 
                    #If the directory already exists, find a unique name 
                    index = 1
                    while os.path.exists(test_dir):
                        test_dir = os.path.join(test_scen_dir, f"test_agent_{agent_name}_{index}")
                        index += 1
                else:

                    os.makedirs(test_dir, exist_ok=True)
                    os.makedirs(os.path.join(test_dir, "plots"), exist_ok=True)

                    summary = pd.DataFrame(columns=["Episode", "Timesteps", "Avg Absolute Path Error", "IAE Cross", "IAE Vertical", "Progression", "Success", "Collision"])
                    summary.to_csv(os.path.join(test_dir, 'test_summary.csv'), index=False)

                    env = gym.make(args.env, scenario=test_scen)
                    agent_model = PPO.load(agent)

                    cum_rewards = {}
                    all_drone_trajs = {}  # Maps episode number to a trajectory
                    all_init_pos = {}  # Maps episode number to initial position
                    for episode in range(args.episodes):
                        try:
                            episode_df, env = simulate_environment(episode, env, agent_model, test_dir, args.save_depth_maps)
                            sim_df = pd.concat([sim_df, episode_df], ignore_index=True)  # TODO make it work with several episodes
                        except NameError:
                            sim_df = episode_df
                        
                        path = env.unwrapped.path

                        all_drone_trajs[episode] = np.stack((episode_df[r"$X$"], episode_df[r"$Y$"], episode_df[r"$Z$"]), axis=-1)
                        all_init_pos[episode] = all_drone_trajs[episode][0]
                        cum_rewards[episode] = episode_df['Reward'].sum()
                        
                        # Per episode stuff
                        drone_traj = np.stack((episode_df[r"$X$"], episode_df[r"$Y$"], episode_df[r"$Z$"]), axis=-1)
                        init_pos = drone_traj[0]
                        obstacles = env.unwrapped.obstacles
                    
                        plotter = Plotter3D(obstacles=obstacles, 
                                            path=path, 
                                            drone_traj=drone_traj,
                                            initial_position=init_pos,
                                            nosave=False)  # TODO make it both save and display interactive plot, needs to fix resolution thing
                        plotter.plot_scene_and_trajs(save_path=os.path.join(test_dir, "plots", f"episode{episode}.png"),
                                                    azimuth=90,  # 90 or 0 is best angle for the 3D plot 
                                                    elevation=None,
                                                    see_from_plane=None)
                        del plotter

                        write_report(test_dir, sim_df, env, episode)
                    
                    if args.episodes > 1:
                        multiplotter = Plotter3DMultiTraj(obstacles=obstacles,
                                                        path=path,
                                                        drone_trajectories=all_drone_trajs,
                                                        cum_rewards=cum_rewards,
                                                        nosave=False)
                        multiplotter.plot_scene_and_trajs(save_path=os.path.join(test_dir, "plots", f"multiplot.png"),
                                                        azimuth=90,
                                                        elevation=None,
                                                        see_from_plane=None)