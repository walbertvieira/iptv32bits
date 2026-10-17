"""Impede que a tela apague / o protetor de tela ligue / o PC suspenda durante a reprodução.

Usa SetThreadExecutionState (kernel32), a mesma API usada por players como VLC e navegadores.
O pedido vale enquanto estiver ativo e é desfeito ao pausar, parar, terminar ou fechar o app —
assim a economia de energia volta a funcionar normalmente fora da reprodução.

Observação: a chamada vale para a thread que a fez; por isso deve ser usada sempre na thread
principal da interface (é o que o player faz).
"""
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class KeepAwake:
    def __init__(self):
        self.active = False
        self._fn = None
        if sys.platform.startswith("win"):
            try:
                import ctypes
                fn = ctypes.windll.kernel32.SetThreadExecutionState
                fn.argtypes = [ctypes.c_uint32]
                fn.restype = ctypes.c_uint32
                self._fn = fn
            except Exception:
                self._fn = None

    @property
    def supported(self) -> bool:
        return self._fn is not None

    def _call(self, flags: int) -> bool:
        if self._fn is None:
            return False
        try:
            return self._fn(flags) != 0
        except Exception:
            return False

    def set(self, on: bool):
        """Liga/desliga o bloqueio. Chamadas repetidas com o mesmo valor não fazem nada."""
        if on == self.active:
            return
        if on:
            self.active = self._call(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)
        else:
            self._call(ES_CONTINUOUS)          # limpa: volta ao comportamento normal do Windows
            self.active = False

    def refresh(self):
        """Reforça o pedido (útil em sessões longas; inofensivo se repetido)."""
        if self.active:
            self._call(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)

    def release(self):
        self.set(False)
