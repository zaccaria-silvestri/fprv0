import serial

from fprv0.transports.base import BaseTransport


class SerialTransport(BaseTransport):
    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 2.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None

    def open(self):
        self.ser = serial.Serial(self.port, baudrate=self.baudrate, timeout=self.timeout)

    def write(self, data: str):
        self.ser.write(data.encode('utf-8'))

    def read_line(self) -> str:
        return self.ser.readline().decode('utf-8').strip()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
