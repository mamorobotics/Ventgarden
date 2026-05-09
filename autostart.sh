source ~/miniconda3/etc/profile.d/conda.sh
cd /home/mhsrobotics/Desktop/Ventgarden/
conda init
conda activate testVent
python vision/rov_dual_viewer.py &
python control/run_controller.py &