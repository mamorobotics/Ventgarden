# Ventgarden

## Overview

The control system is packaged into two parts: one responsible for transmitting camera data, the other responsible for sending control data. 

The camera data is handled through the [µStreamer](https://github.com/pikvm/ustreamer) library. From the topside, seeing the camera is done through [MPV](https://github.com/mpv-player/mpv) and libmpv, which enables low latency and high throughput by using GPU rendering. By using existing libraries, written in C, Ventgarden is able to maintain high performance while still using Python for ease of understanding, maintenance, and future upgrades. 


## Installation & Usage

MPV
Build µStreamer on the ROV side - existing package repos are generally outdated
Python installation

## Todo
- [ ] Implement config.json
- [ ] More debugging details on the controller / serial bridge
- [ ] Uncomment lx and ly values in controllervalues for send partial string (i bypassed this bc my controller has drift)