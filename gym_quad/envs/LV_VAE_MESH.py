import numpy as np
import torch
import trimesh
import gymnasium as gym
import matplotlib.pyplot as plt
import torchvision.transforms as transforms

import gym_quad.utils.geomutils as geom
import gym_quad.utils.state_space as ss
from gym_quad.utils.ODE45JIT import j_Rzyx
from gym_quad.utils.geomutils import enu_to_pytorch3d, enu_to_tri, tri_to_enu
from gym_quad.objects.quad import Quad
from gym_quad.objects.IMU import IMU
from gym_quad.objects.QPMI import QPMI, generate_random_waypoints
from gym_quad.objects.depth_camera import DepthMapRenderer, FoVPerspectiveCameras, RasterizationSettings
from gym_quad.objects.mesh_obstacles import Scene, SphereMeshObstacle

#TODO add stochasticity to make sim2real robust
class LV_VAE_MESH(gym.Env):
    '''Creates an environment where the actionspace consists of Linear velocity and yaw rate which will be passed to a PD or PID controller,
    while the observationspace uses a Varial AutoEncoder "plus more" for observations of environment.'''

    def __init__(self, env_config, scenario="line"):
        # np.random.seed(0) #Uncomment to make the environment deterministic
        print("ENVIRONMENT: LV_VAE_MESH")
        # Set all the parameters from GYM_QUAD/qym_quad/__init__.py as attributes of the class
        for key in env_config:
            setattr(self, key, env_config[key])

        #Actionspace mapped to speed, inclination of velocity vector wrt x-axis and yaw rate
        self.action_space = gym.spaces.Box(
            low = np.array([-1,-1,-1], dtype=np.float32),
            high = np.array([1, 1, 1], dtype=np.float32),
            dtype = np.float32
        )

        #Observationspace
        #Depth camera observation space
        self.perception_space = gym.spaces.Box( #TODO 2x check the shape and type as this is a tensor want it to be a nice tensor for quick processing
            low = 0,
            high = 1,
            shape = (1, self.compressed_depth_map_size, self.compressed_depth_map_size),
            dtype = np.float32
        )

        # IMU observation space
        self.IMU_space = gym.spaces.Box(
            low = -1,
            high = 1,
            shape = (6,),
            dtype = np.float32
        )

        #Domain observation space (Angles, distances and coordinates in body frame)
        self.domain_space = gym.spaces.Box(
            low = -1,
            high = 1,
            shape = (19,),
            dtype = np.float32
        )

        self.observation_space = gym.spaces.Dict({
        'perception': self.perception_space,
        'IMU': self.IMU_space,
        'domain': self.domain_space
        })

        #Scenario set up
        self.scenario = scenario
        self.obstacles = [] #Filled in the scenario functions
        self.scenario_switch = {
            # Training scenarios, all functions defined at the bottom of this file
            "line": self.scenario_line,
            "line_new": self.scenario_line_new,
            "horizontal": self.scenario_horizontal,
            "horizontal_new": self.scenario_horizontal_new,
            # "3d": self.scenario_3d,
            "3d_new": self.scenario_3d_new,
            "easy": self.scenario_easy,
            "helix": self.scenario_helix,
            "intermediate": self.scenario_intermediate,
            "proficient": self.scenario_proficient,
            # "advanced": self.scenario_advanced,
            "expert": self.scenario_expert,
            # Testing scenarios
            "test_path": self.scenario_test_path,
            "test": self.scenario_test,
            "horizontal": self.scenario_horizontal_test,
            "vertical": self.scenario_vertical_test,
            "deadend": self.scenario_deadend_test,
            "crash": self.scenario_dev_test_crash,
        }

        #New init values for sensor using depth camera, mesh and pt3d
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu") #Attempt to use GPU if available

        #Init the quadcopter mesh for collison detection only needed to be done once so it is done here
        self.tri_quad_mesh = trimesh.load("gym_quad/meshes/sphere.obj")
        #resize the quadcopter sphere mesh to have radius r #TODO update this to a hyperparameter like "safety distance"
        r = 1
        self.tri_quad_mesh.apply_scale(r)

        #Reset environment to init state
        self.reset()


    def reset(self,**kwargs):
        """
        Resets environment to initial state.
        """
        seed = kwargs.get('seed', None)
        super().reset(seed=seed)
        # print("PRINTING SEED WHEN RESETTING:", seed) 
        
        #Temp debugging variables
        self.quad_mesh_pos = None

        self.quadcopter = None
        self.path = None
        self.waypoint_index = 0
        self.prog = 0

        self.e = None
        self.h = None
        self.chi_error = None
        self.upsilon_error = None

        self.prev_action = [0,0,0]
        self.prev_quad_pos = None

        self.success = False
        self.done = False

        self.LA_at_end = False
        self.cumulative_reward = 0

        #Obstacle variables
        self.obstacles = []
        self.closest_measurement = None
        self.collided = False

        self.depth_map = torch.zeros((self.depth_map_size[0], self.depth_map_size[1]), dtype=torch.float32, device=self.device)

        self.passed_waypoints = np.zeros((1, 3), dtype=np.float32)
        self.total_t_steps = 0

        ### Path and obstacle generation based on scenario
        scenario = self.scenario_switch.get(self.scenario, lambda: print("Invalid scenario"))
        init_state = scenario() #Called such that the obstacles are generated and the init state of the quadcopter is set

        ## Generate camera, scene and renderer
        camera = None
        raster_settings = None
        scene = None
        if self.obstacles!=[]:

            camera = FoVPerspectiveCameras(device = self.device,fov=self.FOV_vertical)
            raster_settings = RasterizationSettings(
                    image_size=self.depth_map_size, 
                    blur_radius=0.0, 
                    faces_per_pixel=1, # Keep at 1, dont change
                    perspective_correct=True, # Doesn't do anything(??), but seems to improve speed
                    cull_backfaces=True # Do not render backfaces. MAKE SURE THIS IS OK WITH THE GIVEN MESH.
                )
            scene = Scene(device = self.device, obstacles=self.obstacles)

            self.renderer = DepthMapRenderer(device=self.device, 
                                            raster_settings=raster_settings, 
                                            camera=camera, 
                                            scene=scene, 
                                            MAX_MEASURABLE_DEPTH=self.max_depth, 
                                            img_size=self.depth_map_size)
        else: 
            self.depth_map.fill_(self.max_depth)    # IF there are no obstacles then we know that the depthmap always will display max_depth
                                                    # Aditionally we dont need the rasterizer and renderer if there are no obstacles


        #Init the trimesh meshes for collision detection
        obs_meshes = None
        tri_obs_meshes = None
        tri_joined_obs_mesh = None
        if self.obstacles!=[]:
            obs_meshes = [obstacle.mesh for obstacle in self.obstacles] #Extracting the mesh of the obstacles
            tri_obs_meshes = [trimesh.Trimesh(vertices=mesh.verts_packed().cpu().numpy(), faces=mesh.faces_packed().cpu().numpy()) for mesh in obs_meshes] #Converting pt3d meshes to trimesh meshes
            tri_joined_obs_mesh = trimesh.util.concatenate(tri_obs_meshes) #Create one mesh for obstacles
            tri_joined_obs_mesh.fix_normals() #Fixes the normals of the mesh
            self.collision_manager = trimesh.collision.CollisionManager() #Creating the collision manager
            self.collision_manager.add_object("obstacles", tri_joined_obs_mesh) #Adding the obstacles to the collision manager (Stationary objects)
            #Do not add quadcopter to collision manager as it is moving and will be checked in the step function


        # Generate Quadcopter
        self.quadcopter = Quad(self.step_size, init_state)
        self.prev_quad_pos = self.quadcopter.position
        #Move the mesh to the position of the quadcopter
        tri_quad_init_pos = enu_to_tri(self.quadcopter.position)
        #First move the tri_quad_mesh to the origin and then apply the translation
        self.tri_quad_mesh.apply_translation(-self.tri_quad_mesh.centroid)
        self.tri_quad_mesh.apply_translation(tri_quad_init_pos)
        
        self.imu = None
        self.imu = IMU()
        self.imu_measurement = np.zeros((6,), dtype=np.float32) 
        
        ###
        self.info = {}
        self.observation = self.observe() 
        return (self.observation,self.info)


    def observe(self):
        """
        Returns the observations of the environment.
        """
        
        #IMU observation
        self.imu_measurement = self.imu.measure(self.quadcopter)
        #Both linear acceleration and angvel is not in [-1,1] clipping it using the max speed of the quadcopter
        self.imu_measurement[0:3] = self.m1to1(self.imu_measurement[0:3], -self.s_max*2, self.s_max*2)
        self.imu_measurement[3:6] = self.m1to1(self.imu_measurement[3:6], -self.r_max*2, self.r_max*2)
        self.imu_measurement = self.imu_measurement.astype(np.float32)


        #Depth camera observation
        if self.obstacles!=[]:
            pos = self.quadcopter.position
            orientation = self.quadcopter.attitude
            Rcam,Tcam = self.renderer.camera_R_T_from_quad_pos_orient(pos, orientation)
            self.renderer.update_R(Rcam)
            self.renderer.update_T(Tcam)
            self.depth_map = self.renderer.render_depth_map()
            temp_depth_map = self.depth_map
            # print("\nTemp depth map type:", type(temp_depth_map), "  shape:", temp_depth_map.shape, "  dtype:", temp_depth_map.dtype)
        else:
            temp_depth_map = self.depth_map #Handles the case where there are no obstacles

        self.closest_measurement = torch.min(temp_depth_map) #TODO this obly gives the sensors closest but not globally closests might be troublesome for reward calc?
        
        # if self.closest_measurement < self.max_depth:
        #     print("Closest measurement:", self.closest_measurement, "  Max depth:", self.max_depth)
            

        normalized_depth_map = temp_depth_map / self.max_depth
        
        normalized_depth_map_PIL = transforms.ToPILImage()(normalized_depth_map)

        resize_transform = transforms.Compose([
            transforms.Resize((self.compressed_depth_map_size, self.compressed_depth_map_size)),
            transforms.ToTensor(),  # Convert back to tensor
            transforms.Lambda(lambda x: torch.clamp(x, 0, 1))
        ])        

        resized_depth_map = resize_transform(normalized_depth_map_PIL)

        # sensor_readings = resized_depth_map #Might rename sensorreadings to comp_normed_depth_map or VAE_ready_depth_map
        #TODO throws warnings about wanting a np.array instead of a tensor
        #Decide if we let the box change to np.array or if we change the tensor to a np.array here.
        #Migh be unfortunate to change the tensor to np.array here as it will be done every time the observation is called
        #Having to move the tensor to the cpu....
        #Per now we cast to np.array here

        sensor_readings = resized_depth_map.detach().cpu().numpy() 
        self.closest_measurement = self.closest_measurement.item()  #Moves from CPU to GPU if closest meas is on GPU..
        
        #To check if observation is outside bounds
        # if max(sensor_readings.flatten()) > 1 or min(sensor_readings.flatten()) < 0:
        #     print("\nMAX VALUE IN SENSORREADINGS:",max(sensor_readings.flatten()),"\nMIN VALUE IN SENSORREADINGS:", min(sensor_readings.flatten()))

        #Domain observation
        self.update_errors() #Updates the errors chi_error and upsilon_error

        domain_obs = np.zeros(19, dtype=np.float32)
        # Heading angle error wrt. the path
        domain_obs[0] = np.sin(self.chi_error)
        domain_obs[1] = np.cos(self.chi_error)
        # Elevation angle error wrt. the path
        domain_obs[2] = np.sin(self.upsilon_error)
        domain_obs[3] = np.cos(self.upsilon_error)
         
        # x y z of closest point on path in body frame
        relevant_distance = 20 #For this value and lower the observation will be changing i.e. giving info if above or below its clipped to -1 or 1 
        #TODO make this a hypervariable or make it dependent on e.g. the scene

        #OLD WAY to get closest point which was dumb as it causes the optimization in QPMI to be done twice as much as needed
        #As self.prog contains the result from doing this optimization (the u paramter that describes the closest point on the path)
        # x,y,z = self.quadcopter.position
        # closest_point = self.path.get_closest_position([x,y,z], self.waypoint_index) 

        #NEW WAY to get closest point on path using the self.prog variable and using the path __call__ method which turns a u parameter into a point on the path
        closest_point = self.path(self.prog)

        closest_point_body = np.transpose(geom.Rzyx(*self.quadcopter.attitude)).dot(closest_point - self.quadcopter.position)
        domain_obs[4] = self.m1to1(closest_point_body[0], -relevant_distance,relevant_distance) 
        domain_obs[5] = self.m1to1(closest_point_body[1], -relevant_distance, relevant_distance) 
        domain_obs[6] = self.m1to1(closest_point_body[2], -relevant_distance,relevant_distance) 
    
        # Two angles to describe direction of the vector between the drone and the closeset point on path
        x_b_cpp = closest_point_body[0]
        y_b_cpp = closest_point_body[1]
        z_b_cpp = closest_point_body[2]
        ele_closest_p_point_vec = np.arctan2(z_b_cpp, np.sqrt(x_b_cpp**2 + y_b_cpp**2))
        azi_closest_p_point_vec = np.arctan2(y_b_cpp, x_b_cpp)
        
        domain_obs[7] = np.sin(ele_closest_p_point_vec)
        domain_obs[8] = np.cos(ele_closest_p_point_vec)
        domain_obs[9] = np.sin(azi_closest_p_point_vec)
        domain_obs[10] = np.cos(azi_closest_p_point_vec)

        #euclidean norm of the distance from drone to next waypoint
        relevant_distance = (self.path.length / self.n_waypoints-1)*2 #Should be n-1 waypoints to get m segments
        distance_to_next_wp = 0
        try:
            distance_to_next_wp = np.linalg.norm(self.path.waypoints[self.waypoint_index+1] - self.quadcopter.position)
        except IndexError:
            distance_to_next_wp = np.linalg.norm(self.path.waypoints[-1] - self.quadcopter.position)
        domain_obs[11] = self.m1to1(distance_to_next_wp, -relevant_distance, relevant_distance)
        # print("dist_nxt_wp", np.round(distance_to_next_wp),"  normed", np.round(domain_obs[18],2))

        #euclidean norm of the distance from drone to the final waypoint
        distance_to_end = np.linalg.norm(self.path.get_endpoint() - self.quadcopter.position)
        domain_obs[12] = self.m1to1(distance_to_end, -self.path.length*2, self.path.length*2)

        #body coordinates of the look ahead point
        lookahead_world = self.path.get_lookahead_point(self.quadcopter.position, self.la_dist, self.waypoint_index)
        
        #If lookahead point is the end point lock it to the end point
        if not self.LA_at_end and np.abs(lookahead_world[0] - self.path.get_endpoint()[0]) < 1 and np.abs(lookahead_world[1] - self.path.get_endpoint()[1]) < 1 and np.abs(lookahead_world[2] - self.path.get_endpoint()[2]) < 1:
            self.LA_at_end = True
        if self.LA_at_end:
            lookahead_world = self.path.get_endpoint()    

        lookahead_body = np.transpose(geom.Rzyx(*self.quadcopter.attitude)).dot(lookahead_world - self.quadcopter.position)
        relevant_distance = self.la_dist*2 #TODO decide this value
        domain_obs[13] = self.m1to1(lookahead_body[0], -relevant_distance,relevant_distance)
        domain_obs[14] = self.m1to1(lookahead_body[1], -relevant_distance, relevant_distance)
        domain_obs[15] = self.m1to1(lookahead_body[2], -relevant_distance,relevant_distance)

        #Give the previous action as an observation
        domain_obs[16] = self.prev_action[0]    
        domain_obs[17] = self.prev_action[1]
        domain_obs[18] = self.prev_action[2]


        #List of the observations before min-max scaling
        pure_obs = [ 
            *self.imu_measurement,
            self.chi_error,
            self.upsilon_error,
            *closest_point_body,
            ele_closest_p_point_vec,
            azi_closest_p_point_vec,
            distance_to_next_wp,
            distance_to_end,
            *lookahead_body,
            *self.prev_action
        ]

        self.info['pure_obs'] = pure_obs

        #The min max normalized domain observation
        self.info['domain_obs'] = domain_obs

        return {'perception':sensor_readings,
                'IMU':self.imu_measurement,
                'domain':domain_obs}


    def step(self, action):
        """
        Simulates the environment one time-step.
        """
        #Camera is at 15FPS physics is at 100HZ
        #Make the quadcopter step until a new depth map is available
        sim_hz = 1/self.step_size
        cam_hz = self.camera_FPS
        steps_before_new_depth_map = sim_hz//cam_hz
        for i in range(int(steps_before_new_depth_map)):  #TODO update the sim time step inside here???           
            F = self.geom_ctrlv2(action)
            #TODO maybe need some translation between input u and thrust F i.e translate u to propeller speed? 
            #We currently skip this step for simplicity
            self.quadcopter.step(F)

        # Check collision 
        #Do it out here and not inside the loop above to save time and not check every physics sim time step, but every drl time step.
        if self.obstacles != []:
            translation = enu_to_tri(self.quadcopter.position - self.prev_quad_pos)
            self.tri_quad_mesh.apply_translation(translation)
            self.collided = self.collision_manager.in_collision_single(self.tri_quad_mesh)
        self.prev_quad_pos = self.quadcopter.position   
        #Temp save mesh pos for plotting and debugging in run3d.py
        self.quad_mesh_pos = tri_to_enu(self.tri_quad_mesh.vertices[0])

        #Such that the oberservation has access to the previous action
        self.prev_action = action

        self.prog = self.path.get_closest_u(self.quadcopter.position, self.waypoint_index)
        # Check if a waypoint is passed
        k = self.path.get_u_index(self.prog)
        if k > self.waypoint_index:
            print("Passed waypoint {:d}".format(k+1), self.path.waypoints[k], "\tquad position:", self.quadcopter.position)
            self.passed_waypoints = np.vstack((self.passed_waypoints, self.path.waypoints[k]))
            self.waypoint_index = k


        end_cond_1 = np.linalg.norm(self.path.get_endpoint() - self.quadcopter.position) < self.accept_rad # and self.waypoint_index == self.n_waypoints-2 #TODO wwhy this here
        # end_cond_2 = abs(self.prog - self.path.length) <= self.accept_rad/2.0
        end_cond_3 = self.total_t_steps >= self.max_t_steps
        end_cond_4 = self.cumulative_reward < self.min_reward
        # end_cond_4 = False
        if end_cond_1 or end_cond_3 or self.collided or end_cond_4: # or end_cond_2
            if end_cond_1:
                print("Quadcopter reached target!")
                print("Endpoint position", self.path.waypoints[-1], "\tquad position:", self.quadcopter.position) #might need the format line?
                self.success = True
            elif self.collided:
                print("Quadcopter collided!")
                self.success = False
            # elif end_cond_2: #I think this Should be removed such that the quadcopter can fly past the endpoint and come back #TODO
            #     print("Passed endpoint without hitting")
                # print("Endpoint position", self.path.waypoints[-1], "\tquad position:", self.quadcopter.position) #might need the format line?

            elif end_cond_3:
                print("Exceeded time limit")
            elif end_cond_4:
                print("Acumulated reward less than", self.min_reward)
            self.done = True

        # Save sim time info
        self.total_t_steps += 1
        
        #Save interesting info
        self.info['env_steps'] = self.total_t_steps
        self.info['time'] = self.total_t_steps*self.step_size
        self.info['progression'] = self.prog/self.path.length
        self.info['state'] = np.copy(self.quadcopter.state)
        self.info['errors'] = np.array([self.e, self.h])
        self.info['cmd_thrust'] = self.quadcopter.input
        self.info['action'] = action

        # Calculate reward
        step_reward = self.reward()
        self.cumulative_reward += step_reward

        # Make next observation
        self.observation = self.observe()

        #TODO See stack overflow QnA or Sb3 documentation for how to use truncated
        truncated = False
        return self.observation, step_reward, self.done, truncated, self.info


    def reward(self):
        """
        Calculates the reward function for one time step. 
        """
        tot_reward = 0
        lambda_PA = 1
        lambda_CA = 1

        #Path adherence reward
        dist_from_path = np.linalg.norm(self.path(self.prog) - self.quadcopter.position)
        reward_path_adherence = -(2*(np.clip(dist_from_path, 0, self.PA_band_edge) / self.PA_band_edge) - 1)*self.PA_scale 
        # print("reward_path_adherence", np.round(reward_path_adherence,2),\
        #       "  dist_from_path", np.round(dist_from_path,2))
              

        #Path progression reward 
        reward_path_progression = 0
        reward_path_progression1 = np.cos(self.chi_error)*np.linalg.norm(self.quadcopter.velocity)*self.PP_vel_scale
        reward_path_progression2 = np.cos(self.upsilon_error)*np.linalg.norm(self.quadcopter.velocity)*self.PP_vel_scale
        reward_path_progression = reward_path_progression1/2 + reward_path_progression2/2
        reward_path_progression = np.clip(reward_path_progression, self.PP_rew_min, self.PP_rew_max)


        ####Collision avoidance reward#### (continuous)
        #Find the closest obstacle
        reward_collision_avoidance = 0
        if self.obstacles != []: #If there are no obstacles, no need to calculate the reward
            inv_abs_min_rew = self.abs_inv_CA_min_rew 
            danger_range = self.danger_range
            
            #OLD
            # danger_angle = self.danger_angle            
            # quad_pos_torch = torch.tensor(self.quadcopter.position, dtype=torch.float32, device=self.device) 
            # drone_closest_obs_dist = torch.norm(self.nearby_obstacles[0].position - quad_pos_torch).item() - self.nearby_obstacles[0].radius

            drone_closest_obs_dist = self.closest_measurement #TODO now we only use the depth map to determine the closest obstacle Can probably use trimesh to determine the closest obstacle in the mesh
            
            #Determine lambda reward for path following and path adherence based on the distance to the closest obstacle
            #This would benefit from using global information about the obstacles
            #Can let it trickle back to normal based on time assuming that the quadcopter will have moved away from the obstacle
            if (drone_closest_obs_dist < danger_range):
                lambda_PA = (drone_closest_obs_dist/danger_range)/2
                if lambda_PA < 0.10 : lambda_PA = 0.10
                lambda_CA = 1-lambda_PA
            
            #Must give up on the angle diff when using meshes 
            #TODO Can use the positions in the depthmap and penalize the closer and obstacle is to the center.
            #Determine the angle difference between the velocity vector and the vector to the closest obstacle
            # velocity_vec_torch = torch.tensor(self.quadcopter.velocity, dtype=torch.float32, device=self.device)
            # drone_to_obstacle_vec = self.nearby_obstacles[0].position - quad_pos_torch
            # angle_diff = torch.arccos(torch.dot(drone_to_obstacle_vec, velocity_vec_torch)/(torch.norm(drone_to_obstacle_vec)*torch.norm(velocity_vec_torch))).item()

            reward_collision_avoidance = 0
            if (drone_closest_obs_dist < danger_range):
                range_rew = -(((danger_range+inv_abs_min_rew*danger_range)/(drone_closest_obs_dist+inv_abs_min_rew*danger_range)) -1) #same fcns below
                if range_rew > 0: range_rew = 0
                reward_collision_avoidance = range_rew 
            else:
                reward_collision_avoidance = 0
            # print("Collision avoidance reward:", reward_collision_avoidance)
            ####Collision avoidance reward done####

        #Collision reward (sparse)
        reward_collision = 0
        if self.collided:
            reward_collision = self.rew_collision
            print("Collision Reward:", reward_collision)

        #Reach end reward (sparse)
        reach_end_reward = 0
        if self.success:
            reach_end_reward = self.rew_reach_end

        #Existential reward (penalty for being alive to encourage the quadcopter to reach the end of the path quickly) (continous)
        ex_reward = self.existence_reward 

        tot_reward = reward_path_adherence*lambda_PA + reward_collision_avoidance*lambda_CA + reward_collision + reward_path_progression + reach_end_reward + ex_reward

        self.info['reward'] = tot_reward
        self.info['collision_avoidance_reward'] = reward_collision_avoidance*lambda_CA
        self.info['path_adherence'] = reward_path_adherence*lambda_PA
        self.info["path_progression"] = reward_path_progression
        self.info['collision_reward'] = reward_collision
        self.info['reach_end_reward'] = reach_end_reward
        self.info['existence_reward'] = ex_reward
        
        return tot_reward


    def geom_ctrlv2(self, action): #TODO turn into torch operations might speed up the simulation
        #Translate the action to the desired velocity and yaw rate
        cmd_v_x = self.s_max * ((action[0]+1)/2)*np.cos(action[1]*self.i_max)
        cmd_v_y = 0
        cmd_v_z = self.s_max * ((action[0]+1)/2)*np.sin(action[1]*self.i_max)
        cmd_r = self.r_max * action[2]
        self.cmd = np.array([cmd_v_x, cmd_v_y, cmd_v_z, cmd_r]) #For plotting

        #Gains, z-axis-basis=e3 and rotation matrix #TODO add stochasticity to make sim2real robust
        kv = self.kv
        kR = self.kR
        kangvel = self.kangvel

        e3 = np.array([0, 0, 1]) #z-axis basis

        # R = geom.Rzyx(*self.quadcopter.attitude) #OLD
        R = j_Rzyx(*self.quadcopter.attitude)  #NEW using jit version

        #Essentially three different velocities that one can choose to track:
        #Think body or world frame velocity control is the best choice
        ###---###
        ##Vehicle frame velocity control i.e. intermediary frame between body and world frame
        # vehicleR = geom.Rzyx(0, 0, self.quadcopter.attitude[2])
        # vehicle_vels = vehicleR.T @ self.quadcopter.velocity
        # ev = np.array([cmd_v_x, cmd_v_y, cmd_v_z]) - vehicle_vels
        ###---###
        #World frame velocity control
        # ev = np.array([cmd_v_x, cmd_v_y, cmd_v_z]) - self.quadcopter.position_dot

        #Body frame velocity control
        ev = np.array([cmd_v_x, cmd_v_y, cmd_v_z]) - self.quadcopter.velocity
        #Which one to use? vehicle_vels or self.quadcopter.velocity

        #Thrust command (along body z axis)
        f = kv*ev + ss.m*ss.g*e3 + ss.d_w*self.quadcopter.heave*e3
        thrust_command = np.dot(f, R[2])

        #Rd calculation as in Kulkarni aerial gym (works fairly well)
        c_phi_s_theta = f[0]
        s_phi = -f[1]
        c_phi_c_theta = f[2]

        pitch_setpoint = np.arctan2(c_phi_s_theta, c_phi_c_theta)
        roll_setpoint = np.arctan2(s_phi, np.sqrt(c_phi_c_theta**2 + c_phi_s_theta**2))
        yaw_setpoint = self.quadcopter.attitude[2]
        # Rd = geom.Rzyx(roll_setpoint, pitch_setpoint, yaw_setpoint) #OLD
        Rd = j_Rzyx(roll_setpoint, pitch_setpoint, yaw_setpoint) #NEW using jit version


        eR = 1/2*(Rd.T @ R - R.T @ Rd)
        eatt = geom.vee_map(eR)
        eatt = np.reshape(eatt, (3,))

        des_angvel = np.array([0.0, 0.0, cmd_r])

        #Kulkarni approach desired angular rate in body frame:
        s_pitch = np.sin(self.quadcopter.attitude[1])
        c_pitch = np.cos(self.quadcopter.attitude[1])
        s_roll = np.sin(self.quadcopter.attitude[0])
        c_roll = np.cos(self.quadcopter.attitude[0])
        R_euler_to_body = np.array([[1, 0, -s_pitch],
                                    [0, c_roll, s_roll*c_pitch],
                                    [0, -s_roll, c_roll*c_pitch]]) #Uncertain about how this came to be

        des_angvel_body = R_euler_to_body @ des_angvel

        eangvel = self.quadcopter.angular_velocity - R.T @ (Rd @ des_angvel_body) #Kulkarni approach

        torque = -kR*eatt - kangvel*eangvel + np.cross(self.quadcopter.angular_velocity,ss.Ig@self.quadcopter.angular_velocity)

        u = np.zeros(4)
        u[0] = thrust_command
        u[1:] = torque

        F = np.linalg.inv(ss.B()[2:]).dot(u)
        F = np.clip(F, ss.thrust_min, ss.thrust_max)
        return F


    #### UTILS ####
    def m1to1(self,value, min, max): 
        '''
        Normalizes a value from the range [min,max] to the range [-1,1]
        If value is outside the min max range, it will be clipped to the min or max value (ensuring we return a value in the range [-1,1])
        '''
        value_normalized = 2.0*(value-min)/(max-min) - 1
        return np.clip(value_normalized, -1, 1)

    def invm1to1(self, value, min, max):
        '''
        Inverse normalizes a value from the range [-1,1] to the range [min,max]
        If value that got normalized was outside the min max range it may only be inverted to the min or max value (will not be correct if the clip was used in the normalization)
        '''
        return (value+1)*(max-min)/2.0 + min


    #### UPDATE FUNCTION####
    def update_errors(self): #TODO these dont need to be self.variables and should rather be returned
        '''Updates the cross track and vertical track errors, as well as the course and elevation errors.'''
        self.e = 0.0 #Cross track error
        self.h = 0.0 #Vertical track error
        self.chi_error = 0.0 #Course angle error xy-plane
        self.upsilon_error = 0.0 #Elevation angle error between z and xy-plane

        s = self.prog

        chi_p, upsilon_p = self.path.get_direction_angles(s) #Path direction angles also denoted by pi in some literature
        # Calculate tracking errors Serret Frenet frame
        SF_rotation = geom.Rzyx(0, upsilon_p, chi_p)

        epsilon = np.transpose(SF_rotation).dot(self.quadcopter.position - self.path(s))
        self.e = epsilon[1] #Cross track error
        self.h = epsilon[2] #Vertical track error

        # Calculate course and elevation errors from tracking errors
        chi_r = np.arctan2(self.e, self.la_dist) 
        upsilon_r = np.arctan2(self.h, np.sqrt(self.e**2 + self.la_dist**2))
        
        #Desired course and elevation angles
        chi_d = chi_p - chi_r 
        upsilon_d = upsilon_p - upsilon_r 

        self.chi_error = geom.ssa(chi_d - self.quadcopter.chi) #Course angle error xy-plane 
        self.upsilon_error = geom.ssa(upsilon_d - self.quadcopter.upsilon) #Elevation angle error between z and xy-plane

        # print("upsilon_d", np.round(upsilon_d*180/np.pi), "upsilon_quad", np.round(self.quadcopter.upsilon*180/np.pi), "upsilon_error", np.round(self.upsilon_error*180/np.pi),\
        #       "\n\nchi_d", np.round(chi_d*180/np.pi), "chi_quad", np.round(self.quadcopter.chi*180/np.pi), "chi_error", np.round(self.chi_error*180/np.pi))

    #### PLOTTING ####
    def axis_equal3d(self, ax):
        """
        Shifts axis in 3D plots to be equal. Especially useful when plotting obstacles, so they appear spherical.

        Parameters:
        ----------
        ax : matplotlib.axes
            The axes to be shifted.
        """
        extents = np.array([getattr(ax, 'get_{}lim'.format(dim))() for dim in 'xyz'])
        sz = extents[:,1] - extents[:,0]
        centers = np.mean(extents, axis=1)
        maxsize = max(abs(sz))
        r = maxsize/2
        for ctr, dim in zip(centers, 'xyz'):
            getattr(ax, 'set_{}lim'.format(dim))(ctr - r, ctr + r)
        # plt.show()
        return ax

    def plot3D(self, wps_on=True, leave_out_first_wp=True):
        """
        Returns 3D plot of path and obstacles.
        """
        ax = self.path.plot_path(wps_on, leave_out_first_wp=leave_out_first_wp)
        for obstacle in self.obstacles:
            ax.plot_surface(*obstacle.return_plot_variables(), color='r', zorder=1)
            ax.set_aspect('equal', adjustable='datalim')
        return ax#self.axis_equal3d(ax)

    def plot_section3d(self):
        """
        Returns 3D plot of path, obstacles and quadcopter.
        """
        plt.rc('lines', linewidth=3)
        ax = self.plot3D(wps_on=False)
        ax.set_xlabel(xlabel="North [m]", fontsize=14)
        ax.set_ylabel(ylabel="East [m]", fontsize=14)
        ax.set_zlabel(zlabel="Down [m]", fontsize=14)
        ax.xaxis.set_tick_params(labelsize=12)
        ax.yaxis.set_tick_params(labelsize=12)
        ax.zaxis.set_tick_params(labelsize=12)
        ax.set_xticks([0, 50, 100])
        ax.set_yticks([-50, 0, 50])
        ax.set_zticks([-50, 0, 50])
        ax.view_init(elev=-165, azim=-35)
        ax.scatter3D(*self.quadcopter.position, label="Initial Position", color="y")

        self.axis_equal3d(ax)
        ax.legend(fontsize=14)
        plt.show()


    #### SCENARIOS #### 
    
    #Utility function for scenarios
    def check_object_overlap(self, new_obstacle): #TODO Potentially drop if general meshes are used
        """
        Checks if a new obstacle is overlapping one that already exists or the target position.
        """
        overlaps = False
        # check if it overlaps target:
        endpoint_torch = torch.tensor(self.path.get_endpoint(),device=self.device).float()
        if torch.norm(endpoint_torch - new_obstacle.position) < new_obstacle.radius + 5:
            return True
        # check if it overlaps already placed objects
        for obstacle in self.obstacles:
            if torch.norm(obstacle.position - new_obstacle.position) < new_obstacle.radius + obstacle.radius + 5:
                overlaps = True
        return overlaps
    
    def generate_obstacles(self, n, rmin, rmax , path:QPMI, mean, std, onPath=False): #TODO make into torch?
        '''
        Inputs:
        n: number of obstacles
        rmin: minimum radius of obstacles
        rmax: maximum radius of obstacles
        path: path object
        mean: mean distance from path
        std: standard deviation of distance from path
        onPath: if True, obstacles will be placed on the path
        Returns:
        obstaclecoords: list of obstacle coordinates
        '''
        num_obstacles = 0
        path_lenght = path.length
        while num_obstacles < n:
            #uniform distribution of length along path
            u_obs = np.random.uniform(0.20*path_lenght,0.90*path_lenght)
            #get path angle at u_obs
            path_angle = path.get_direction_angles(u_obs)[0]
            #Draw a normal distributed random number for the distance from the path
            dist = np.random.normal(mean, std)
            #get x,y,z coordinates of the obstacle if it were placed on the path
            x,y,z = path.__call__(u_obs)
            obs_on_path_pos = np.array([x,y,z])
            #offset the obstacle from the path 90 degrees normal on the path
            obs_pos = obs_on_path_pos + dist*np.array([np.cos(path_angle-np.pi/2),np.sin(path_angle-np.pi/2),0])

            obstacle_radius = np.random.uniform(rmin,rmax) #uniform distribution of size
            if np.linalg.norm(obs_pos - obs_on_path_pos) > obstacle_radius+2 and not onPath: #2 is a safety margin   
                obstacle_coords = torch.tensor(obs_pos,device=self.device).float().squeeze()
                pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
                self.obstacles.append(SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))
                num_obstacles += 1
            elif onPath:   
                obstacle_coords = torch.tensor(obs_pos,device=self.device).float().squeeze()
                pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
                self.obstacles.append(SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))
                num_obstacles += 1
            else:
                continue

    #No obstacles
    def scenario_line(self):
        initial_state = np.zeros(6)
        waypoints = generate_random_waypoints(self.n_waypoints,'line')
        self.path = QPMI(waypoints)
        # init_pos = [np.random.uniform(0,2)*(-5),0, 0]#np.random.normal(0,1)*5]
        init_pos = [0, 0, 0]#np.random.normal(0,1)*5]
        #init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        init_attitude=np.array([0,0,self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos, init_attitude])
        return initial_state

    def scenario_line_new(self):
        initial_state = np.zeros(6)
        waypoints = generate_random_waypoints(self.n_waypoints,'line_new')
        self.path = QPMI(waypoints)
        # init_pos = [np.random.uniform(0,2)*(-5),0, 0]#np.random.normal(0,1)*5]
        init_pos = [0, 0, 0]#np.random.normal(0,1)*5]
        #init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        init_attitude=np.array([0,0,self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos, init_attitude])
        return initial_state

    def scenario_horizontal(self):
        initial_state = np.zeros(6)
        waypoints = generate_random_waypoints(self.n_waypoints,'horizontal')
        self.path = QPMI(waypoints)
        # init_pos = [np.random.uniform(0,2)*(-5), np.random.normal(0,1)*5, 0]#np.random.normal(0,1)*5]
        init_pos = [0, 0, 0]#np.random.normal(0,1)*5]
        #init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        init_attitude=np.array([0,0,self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos, init_attitude])
        return initial_state

    def scenario_horizontal_new(self):
        initial_state = np.zeros(6)
        waypoints = generate_random_waypoints(self.n_waypoints,'horizontal_new')
        self.path = QPMI(waypoints)
        # init_pos = [np.random.uniform(0,2)*(-5), np.random.normal(0,1)*5, 0]#np.random.normal(0,1)*5]
        init_pos = [0, 0, 0]#np.random.normal(0,1)*5]
        #init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        init_attitude=np.array([0,0,self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos, init_attitude])
        return initial_state

    # def scenario_3d(self):
    #     initial_state = np.zeros(6)
    #     waypoints = generate_random_waypoints(self.n_waypoints,'3d')
    #     self.path = QPMI(waypoints)
    #     init_pos = [np.random.uniform(0,2)*(-5), np.random.normal(0,1)*5, np.random.normal(0,1)*5]
    #     #init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
    #     init_attitude=np.array([0, 0, self.path.get_direction_angles(0)[0]])
    #     initial_state = np.hstack([init_pos, init_attitude])
    #     return initial_state

    def scenario_3d_new(self):
        initial_state = np.zeros(6)
        waypoints = generate_random_waypoints(self.n_waypoints,'3d_new')
        self.path = QPMI(waypoints)
        # init_pos=[-10, -10, 0]
        init_pos = [np.random.uniform(-10,10), np.random.uniform(-10,10), np.random.uniform(-10,10)]
        init_pos=[0, 0, 0]
        init_attitude=np.array([0, 0, self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos, init_attitude])
        return initial_state

    #With obstacles

    def scenario_easy(self): #Surround the path with 1-4 obstacles But ensure no obstacles on path
        initial_state = self.scenario_3d_new()
        n_obstacles = np.random.randint(1,5)
        self.generate_obstacles(n = n_obstacles, rmin=2, rmax=6, path = self.path, mean = 0, std = 5, onPath=False)
        return initial_state

    def scenario_intermediate(self):
        initial_state = self.scenario_3d_new()
        obstacle_radius = np.random.uniform(low=4,high=10)
        obstacle_coords = self.path(self.path.length/2)# + np.random.uniform(low=-obstacle_radius, high=obstacle_radius, size=(1,3))
        
        obstacle_coords = torch.tensor(obstacle_coords,device=self.device).float().squeeze()
        pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
        self.obstacles.append(SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))
        return initial_state

    def scenario_proficient(self):
        initial_state = self.scenario_3d_new()
        obstacle_radius = np.random.uniform(low=4,high=10)
        obstacle_coords = self.path(self.path.length/2)# + np.random.uniform(low=-obstacle_radius, high=obstacle_radius, size=(1,3))
        obstacle_coords = torch.tensor(obstacle_coords,device=self.device).float().squeeze() #go from [[x,y,z]] to [x,y,z]
        pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
        self.obstacles.append(SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))

        lengths = np.linspace(self.path.length*1/6, self.path.length*5/6, 2)
        for l in lengths:
            obstacle_radius = np.random.uniform(low=4,high=10)
            obstacle_coords = self.path(l) + np.random.uniform(low=-(obstacle_radius+10), high=(obstacle_radius+10), size=(1,3))
            obstacle_coords = torch.tensor(obstacle_coords,device=self.device).float().squeeze() #TODO apply squeeze to all other obstacle_coords that are [[]] and not []
            pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
            obstacle = SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path)
            
            if self.check_object_overlap(obstacle):
                continue
            else:
                self.obstacles.append(obstacle)
        return initial_state


    # def scenario_advanced(self): #IDK WHY COMMENTED OUT
    #     initial_state = self.scenario_proficient()
    #     while len(self.obstacles) < self.n_adv_obstacles: # Place the rest of the obstacles randomly
    #         s = np.random.uniform(self.path.length*1/3, self.path.length*2/3)
    #         obstacle_radius = np.random.uniform(low=4,high=10)
    #         obstacle_coords = self.path(s) + np.random.uniform(low=-(obstacle_radius+10), high=(obstacle_radius+10), size=(1,3))
    #         obstacle = Obstacle(obstacle_radius, obstacle_coords[0])
    #         if self.check_object_overlap(obstacle):
    #             continue
    #         else:
    #             self.obstacles.append(obstacle)
    #     return initial_state

    def scenario_expert(self):
        initial_state = self.scenario_3d_new()
        obstacle_radius = np.random.uniform(low=4,high=10)
        obstacle_coords = self.path(self.path.length/2)# + np.random.uniform(low=-obstacle_radius, high=obstacle_radius, size=(1,3))
        obstacle_coords = torch.tensor(obstacle_coords,device=self.device).float().squeeze()
        pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
        self.obstacles.append(SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))

        lengths = np.linspace(self.path.length*1.5/6, self.path.length*5/6, 5)
        for l in lengths:
            obstacle_radius = np.random.uniform(low=4,high=10)
            obstacle_coords = self.path(l) + np.random.uniform(low=-(obstacle_radius+10), high=(obstacle_radius+10), size=(1,3))
            obstacle_coords = torch.tensor(obstacle_coords,device=self.device).float().squeeze()
            pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)            
            obstacle = SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path)
            if self.check_object_overlap(obstacle):
                continue
            else:
                self.obstacles.append(obstacle)

        return initial_state

    def scenario_test_path(self):
        # test_waypoints = np.array([np.array([0,0,0]), np.array([1,1,0]), np.array([9,9,0]), np.array([10,10,0])])
        # test_waypoints = np.array([np.array([0,0,0]), np.array([5,0,0]), np.array([10,0,0]), np.array([15,0,0])])
        test_waypoints = np.array([np.array([0,0,0]), np.array([10,1,0]), np.array([20,0,0]), np.array([70,0,0])])
        self.n_waypoints = len(test_waypoints)
        self.path = QPMI(test_waypoints)
        init_pos = [0,0,0]
        init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos, init_attitude])

        obstacle_radius = 10
        obstacle_coords = self.path(20)
        obstacle_coords = torch.tensor(obstacle_coords,device=self.device).float().squeeze()
        pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
        self.obstacles.append(SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))
        return initial_state

    def scenario_test(self):
        initial_state = self.scenario_test_path()
        obstacle_radius = 10
        obstacle_coords = self.path(self.path.length/2)
        obstacle_coords = torch.tensor(obstacle_coords,device=self.device).float().squeeze()
        pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
        self.obstacles.append(SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))
        return initial_state

    def scenario_horizontal_test(self):
        waypoints = [(0,0,0), (50,0.1,0), (100,0,0)]
        self.path = QPMI(waypoints)
        self.obstacles = []
        for i in range(7):
            y = -30+10*i
            obstacle_coords = torch.tensor([50,y,0],device=self.device)
            pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
            self.obstacles.append(SphereMeshObstacle(radius = 5, center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))
            
        init_pos = np.array([0, 0, 0]) + np.random.uniform(low=-5, high=5, size=(1,3))
        init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos[0], init_attitude])
        return initial_state

    def scenario_vertical_test(self):
        waypoints = [(0,0,0), (50,0,1), (100,0,0)]
        self.path = QPMI(waypoints)
        self.obstacles = []
        for i in range(7):
            z = -30+10*i
            obstacle_coords = torch.tensor([50,0,z],device=self.device)
            pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
            self.obstacles.append(SphereMeshObstacle(radius = 5,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))
        init_pos = np.array([0, 0, 0]) + np.random.uniform(low=-5, high=5, size=(1,3))
        init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos[0], init_attitude])
        return initial_state

    def scenario_deadend_test(self):
        waypoints = [(0,0,0), (50,0.5,0), (100,0,0)]
        self.path = QPMI(waypoints)
        radius = 10
        angles = np.linspace(-90, 90, 10)*np.pi/180
        obstacle_radius = (angles[1]-angles[0])*radius/2
        for ang1 in angles:
            for ang2 in angles:
                x = 45 + radius*np.cos(ang1)*np.cos(ang2)
                y = radius*np.cos(ang1)*np.sin(ang2)
                z = -radius*np.sin(ang1)
                
                obstacle_coords = torch.tensor([x,y,z],device=self.device).float().squeeze()
                pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
                self.obstacles.append(SphereMeshObstacle(radius = obstacle_radius,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))

        init_pos = np.array([0, 0, 0]) + np.random.uniform(low=-5, high=5, size=(1,3))
        init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos[0], init_attitude])
        return initial_state

    def scenario_helix(self):
        initial_state = np.zeros(6)
        waypoints = generate_random_waypoints(self.n_waypoints,'helix')
        self.path = QPMI(waypoints)
        # init_pos = helix_param(0)
        init_pos = np.array([110, 0, -26]) + np.random.uniform(low=-5, high=5, size=(1,3))
        init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos[0], init_attitude])
        obstacle_coords = torch.tensor([0,0,0],device=self.device).float()
        pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
        self.obstacles.append(SphereMeshObstacle(radius = 100,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))

        return initial_state

    #Development scenarios
    def scenario_dev_test_crash(self):
        initial_state = np.zeros(6)
        waypoints = generate_random_waypoints(3,'line')
        self.path = QPMI(waypoints)
        init_pos = [0, 0, 0]
        init_attitude = np.array([0, self.path.get_direction_angles(0)[1], self.path.get_direction_angles(0)[0]])
        initial_state = np.hstack([init_pos, init_attitude])
        #Place one large obstacle at the second waypoint
        obstacle_coords = torch.tensor(self.path.waypoints[1],device=self.device).float().squeeze()
        pt3d_obs_coords = enu_to_pytorch3d(obstacle_coords,device=self.device)
        self.obstacles.append(SphereMeshObstacle(radius = 20,center_position=pt3d_obs_coords,device=self.device,path=self.mesh_path))
        return initial_state
