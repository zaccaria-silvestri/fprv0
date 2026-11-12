import pyvisa

from fprv0.transports.base import BaseTransport


class VISATransport(BaseTransport):
    def __init__(self, address: str, timeout: int = 5000):
        self.address = address
        self.timeout = timeout
        self.rm = None
        self.inst = None

    def open(self):
        self.rm = pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(self.address)
        self.inst.timeout = self.timeout

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd)

    def write(self, cmd: str):
        self.inst.write(cmd)

    def close(self):
        if self.inst:
            self.inst.close()
        if self.rm:
            self.rm.close()
