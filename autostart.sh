cd /home/mamorobotics/Desktop/Ventgarden/
conda init
conda activate testVent
python vision/rov_dual_viewer.py &
python control/run_controller.py &