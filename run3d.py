import numpy as np
import matplotlib.pyplot as plt
import gym
import gym_quad
import os

from gym_quad.utils.controllers import PI, PID
from mpl_toolkits.mplot3d import Axes3D
from stable_baselines3 import PPO
from utils import *


os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 


if __name__ == "__main__":
    experiment_dir, agent_path, args = parse_experiment_info()
    env = gym.make(args.env, scenario=args.scenario)
    agent = PPO.load(agent_path)
    sim_df,_,_ = simulate_environment(env, agent)
    sim_df.to_csv(r'simdata.csv')
    calculate_IAE(sim_df)
    plot_attitude(sim_df)
    #plot_velocity(sim_df)
    #plot_angular_velocity(sim_df)
    #plot_control_inputs([sim_df])
    #plot_control_errors([sim_df])
    plot_3d(env, sim_df)
    #plot_current_data(sim_df)

