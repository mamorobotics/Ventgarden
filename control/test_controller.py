from controller_serial import GameController
from run_controller import load_config
import time

config = load_config('config.json')

c = GameController(config['controller'])
send_interval = config["controller"].get("send_interval_ms", 100) / 1000.0

if c.connect():
    print('Controller connected!')
else:
    print('Failed to connect controller')


while True:
    values = c.get_values()
    print(values)
    time.sleep(send_interval)
    


