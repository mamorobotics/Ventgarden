# Ventgarden

## Overview

The control system is packaged into two parts: one responsible for transmitting camera data, the other responsible for sending control data. 

The camera data is handled through the [µStreamer](https://github.com/pikvm/ustreamer) library. From the topside, seeing the camera is done through [MPV](https://github.com/mpv-player/mpv) and libmpv, which enables low latency and high throughput by using GPU rendering. By using existing libraries, written in C, Ventgarden is able to maintain high performance while still using Python for ease of understanding, maintenance, and future upgrades. 


## Installation & Usage

MPV
Build µStreamer on the ROV side - existing package repos are generally outdated
Python installation

Ensure you have Anaconda / Miniconda installed. 

Then, create the conda environment based on the environment.yml file. 

`conda env create -f environment.yml`

Then, activate the environment `conda activate VentgardenENV`

You should be ready to run the relevant python files!

## Todo
- [ ] Implement config.json
- [ ] More debugging details on the controller / serial bridge
- [ ] Uncomment lx and ly values in controllervalues for send partial string (i bypassed this bc my controller has drift)
- [ ] **(IMPORTANT)** Write a simple python script which can detect when the ustreamer server on the bot side is not available / has disconnected, and logs it to a file / the command line. Basically, we have had issues with the ethernet connection disconnecting / crashing, and I think I have fixed it, but we need to be sure. 
- [ ] Migrate from sending controller data to just PWM signals (see Arduino-Control repo and the control folder for more info)
- [ ] Add 2 camera functionality (side by side, switch between cameras on the main viewer, implement throtlling for dual camera vision).
- [ ] Implement still capture / video capture
- [ ] Float control + data
