import imageio.v2 as imageio


# Define filetype of source images:
extension = '.png'

# Option A: N filenames have common structure and are numbered in the correct sequence:
#	    "image_name_0.png", "image_name_1.png", "image_name_2.png", ...
# 	    n_imgs: known number of images
# n_imgs = 24
# path = "gym_quad/tests/test_img/depth_maps/"
# filenames = [f"{path}depth_map{i}{extension}" for i in range(n_imgs)]

# Option B: unknown filenames, but they are still ordered in ascending order by name:
import glob

filenames = sorted(glob.glob('log/LV_VAE-v0/Experiment 1/test24/depth_maps/depth_map_*.png'))
print("there are ", len(filenames), " images in the folder\nBeginning to create gif... ",end="")

# Read, compose and write images to .gif:
with imageio.get_writer('my_image_animation.gif', mode='I', duration=0.01, loop=0) as writer:
    for filename in filenames:
        image = imageio.imread(filename)
        writer.append_data(image)

print("GIF created successfully!")
#TODO make resolution of gif better?