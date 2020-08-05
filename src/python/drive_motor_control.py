import struct
from math import pi
from threading import Event, RLock, Thread
from typing import NamedTuple, Union, Optional, Literal

import serial

# Wheel constants
ENCODER_PPR = 2912
WHEEL_DIA_CM = 13.5
WHEEL_CIR_CM = pi * WHEEL_DIA_CM
WHEEL_CM_PER_TICK = WHEEL_CIR_CM / ENCODER_PPR
WHEEL_TICK_PER_CM = ENCODER_PPR / WHEEL_CIR_CM
WHEEL_TRACK_CM = 28

# Types
Real = Union[float, int]


class DriveMotorException(Exception):
    pass


class DriveMotorState(NamedTuple):
    left_motor_position: Real
    left_motor_velocity: Real
    right_motor_position: Real
    right_motor_velocity: Real
    left_bumper: bool
    middle_bumper: bool
    right_bumper: bool


class DriveMotorController(Thread):
    _ACK = b'\xAA'
    _SET_VELOCITY_COMMAND = b'\xC0'
    _SET_VELOCITY_RECV_STRUCT = struct.Struct('>chh')
    _SET_VELOCITY_SEND_STRUCT = struct.Struct('>lhlhc')

    def __init__(
            self,
            controller_serial_port: str = '/dev/ttyACM0',
            baudrate: int = 115200,
            set_velocity_resend_period: Optional[float] = 1.0
    ) -> None:
        super().__init__(name='DriveMotorControllerThread', daemon=True)

        self._conn = serial.Serial(port=controller_serial_port, baudrate=baudrate)
        self._lock = RLock()
        self._set_velocity_resend_period = set_velocity_resend_period
        self._stop_event = Event()

        self.target_left_motor_velocity = 0.0
        self.target_right_motor_velocity = 0.0

        self.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop_event.set()

        try:
            self.stop_motors()
        finally:
            self.join()

            with self._lock:
                self._conn.close()

    def get_drive_motor_state(self) -> DriveMotorState:
        return self._send_set_velocity_command()

    def run(self) -> None:
        if self._set_velocity_resend_period is None or self._set_velocity_resend_period <= 0:
            return

        while not self._stop_event.wait(self._set_velocity_resend_period):
            self._send_set_velocity_command()

    def set_velocity_differential(
            self,
            left_motor_velocity: Real = None,
            right_motor_velocity: Real = None,
            distance_unit: Literal['cm', 'ticks'] = 'cm'
    ) -> DriveMotorState:
        if distance_unit == 'cm':
            scalar = WHEEL_TICK_PER_CM
        elif distance_unit == 'ticks':
            scalar = 1
        else:
            raise ValueError(f'Unrecognized distance_unit "{distance_unit}".')

        with self._lock:
            if left_motor_velocity is not None:
                self.target_left_motor_velocity = left_motor_velocity * scalar

            if right_motor_velocity is not None:
                self.target_right_motor_velocity = right_motor_velocity * scalar

            return self._send_set_velocity_command()

    def set_velocity_steer(self, linear_velocity: Real, angular_velocity: Real) -> DriveMotorState:
        """
        :param linear_velocity: [cm / s]
        :param angular_velocity: [deg / s]
        """

        # a = (WHEEL_TRACK_CM / 100) / 2
        # Had to determine the 5 / 6 heuristically :/
        a = (WHEEL_TRACK_CM / 100) * (5 / 6)
        left_motor_velocity = linear_velocity - angular_velocity * a
        right_motor_velocity = linear_velocity + angular_velocity * a

        return self.set_velocity_differential(left_motor_velocity, right_motor_velocity, distance_unit='cm')

    def stop_motors(self) -> DriveMotorState:
        return self.set_velocity_differential(0, 0)

    def _send_set_velocity_command(self) -> DriveMotorState:
        with self._lock:
            send_data = self._SET_VELOCITY_RECV_STRUCT.pack(
                self._SET_VELOCITY_COMMAND,
                int(round(self.target_left_motor_velocity)),
                int(round(self.target_right_motor_velocity))
            )

            self._conn.write(send_data)
            self._conn.flush()

            response = self._conn.read()
            if response != self._ACK:
                raise DriveMotorException('Did not receive ACK from the controller!')

            left_motor_position, left_motor_velocity, right_motor_position, right_motor_velocity, bumpers = \
                self._SET_VELOCITY_SEND_STRUCT.unpack(self._conn.read(13))

        bumpers = bumpers[0]
        left_bumper = bool(bumpers & 0x04)
        middle_bumper = bool(bumpers & 0x02)
        right_bumper = bool(bumpers & 0x01)

        return DriveMotorState(
            left_motor_position,
            left_motor_velocity,
            right_motor_position,
            right_motor_velocity,
            left_bumper,
            middle_bumper,
            right_bumper
        )


def test_set_velocity_differential_gamesir(drive_motors: DriveMotorController):
    import gamesir

    normal_speed = 50
    turbo_speed = 100
    turbo = False

    print('Connecting to GameSir controller...')
    controller = gamesir.get_controllers()[0]
    print('Connected.')

    lv_scale = rv_scale = 0
    for event in controller.read_loop():
        if event.type not in controller.EVENT_TYPES:
            continue

        event_code = controller.EventCode(event.code)

        if event_code == controller.EventCode.LEFT_JOYSTICK_Y:
            value = event.value
            # Scale from [255, 0] to [-1., 1.]
            lv_scale = - (value - 128) / 128

        elif event_code == controller.EventCode.RIGHT_JOYSTICK_Y:
            value = event.value
            # Scale from [255, 0] to [-1., 1.]
            rv_scale = - (value - 128) / 128

        elif event_code == controller.EventCode.RIGHT_TRIGGER_PRESSURE:
            turbo = event.value >= 200

        else:
            continue

        max_speed = turbo_speed if turbo else normal_speed
        lv = max_speed * lv_scale
        rv = max_speed * rv_scale

        print(f'LV: {lv} \tRV: {rv}')
        print(drive_motors.set_velocity_differential(lv, rv))


def test_set_velocity_steer_gamesir(drive_motors: DriveMotorController):
    import gamesir

    normal_speed = 40
    turbo_speed = 80
    turbo = False

    print('Connecting to GameSir controller...')
    controller = gamesir.get_controllers()[0]
    print('Connected.')

    v_scale = w_scale = 0
    for event in controller.read_loop():
        if event.type not in controller.EVENT_TYPES:
            continue

        event_code = controller.EventCode(event.code)

        if event_code == controller.EventCode.LEFT_JOYSTICK_Y:
            value = event.value
            # Scale from [255, 0] to [-1., 1.]
            v_scale = - (value - 128) / 128

        elif event_code == controller.EventCode.LEFT_JOYSTICK_X:
            value = event.value
            # Scale from [255, 0] to [-1., 1.]
            w_scale = - (value - 128) / 128

        elif event_code == controller.EventCode.RIGHT_TRIGGER_PRESSURE:
            turbo = event.value >= 200

        else:
            continue

        max_speed = turbo_speed if turbo else normal_speed

        v = max_speed * v_scale
        w = 90 * w_scale
        print(f'v: {v} \tw: {w}')
        print(drive_motors.set_velocity_steer(v, w if v >= 0 else -w))
        print(drive_motors.set_velocity_steer(v, w))


def test_set_velocity_steer_cli(drive_motors: DriveMotorController):
    while True:
        try:
            result = input('[v,w]: ')
            v, w = result.split(',')
            v, w = float(v), float(w)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(e)
            continue

        print(drive_motors.set_velocity_steer(v, w))


def main():
    print('Connecting to drive motor controller...')
    with DriveMotorController() as drive_motors:
        print('Connected.\n')

        # test_set_velocity_steer_cli(drive_motors)
        test_set_velocity_steer_gamesir(drive_motors)


if __name__ == '__main__':
    main()
