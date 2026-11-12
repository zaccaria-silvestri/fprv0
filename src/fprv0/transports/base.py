from abc import ABC, abstractmethod


class BaseTransport(ABC):
    @abstractmethod
    def open(self):
        pass

    @abstractmethod
    def close(self):
        pass
