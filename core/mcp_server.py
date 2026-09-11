"""VoicePet 本地 MCP stdio 服务。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import webbrowser
import zipfile
import glob
from pathlib import Path
from urllib.parse import urlparse

TOOLS = [
    {"name":"open_url","description":"用默认浏览器打开 HTTP/HTTPS 地址","inputSchema":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}},
    {"name":"launch_app","description":"启动应用程序","inputSchema":{"type":"object","properties":{"program":{"type":"string"},"args":{"type":"array","items":{"type":"string"}}},"required":["program"]}},
    {"name":"open_folder","description":"打开文件夹或在资源管理器定位路径","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"select":{"type":"boolean"}},"required":["path"]}},
    {"name":"delete_file","description":"删除单个文件，可移入回收站","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"use_recycle_bin":{"type":"boolean"}},"required":["path"]}},
    {"name":"copy_file","description":"复制文件或目录","inputSchema":{"type":"object","properties":{"source":{"type":"string"},"destination":{"type":"string"}},"required":["source","destination"]}},
    {"name":"move_file","description":"移动文件或目录","inputSchema":{"type":"object","properties":{"source":{"type":"string"},"destination":{"type":"string"}},"required":["source","destination"]}},
    {"name":"rename_file","description":"重命名文件或目录","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"new_name":{"type":"string"}},"required":["path","new_name"]}},
    {"name":"file_info","description":"查询文件信息","inputSchema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name":"create_folder","description":"创建文件夹","inputSchema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name":"search_files","description":"搜索文件","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"pattern":{"type":"string"},"recursive":{"type":"boolean"}},"required":["path","pattern"]}},
    {"name":"zip_files","description":"压缩文件或目录","inputSchema":{"type":"object","properties":{"paths":{"type":"array","items":{"type":"string"}} ,"destination":{"type":"string"}},"required":["paths","destination"]}},
    {"name":"unzip_file","description":"解压 ZIP 文件","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"destination":{"type":"string"}},"required":["path","destination"]}},
    {"name":"open_mailto","description":"打开邮件撰写窗口","inputSchema":{"type":"object","properties":{"address":{"type":"string"},"subject":{"type":"string"},"body":{"type":"string"}},"required":["address"]}},
    {"name":"open_tel","description":"打开电话协议","inputSchema":{"type":"object","properties":{"number":{"type":"string"}},"required":["number"]}},
    {"name":"get_system_info","description":"查询系统信息","inputSchema":{"type":"object","properties":{}}},
    {"name":"get_network_status","description":"查询网络状态","inputSchema":{"type":"object","properties":{}}},
    {"name":"get_battery_status","description":"查询电池状态","inputSchema":{"type":"object","properties":{}}},
    {"name":"lock_screen","description":"锁定 Windows 会话","inputSchema":{"type":"object","properties":{}}},
    {"name":"sleep_computer","description":"让电脑睡眠","inputSchema":{"type":"object","properties":{}}},
    {"name":"shutdown_computer","description":"关机、重启或取消关机","inputSchema":{"type":"object","properties":{"action":{"type":"string","enum":["shutdown","restart","cancel"]},"delay_seconds":{"type":"integer"}},"required":["action"]}},
    {"name":"clipboard_read","description":"读取剪贴板文本","inputSchema":{"type":"object","properties":{}}},
    {"name":"clipboard_write","description":"写入剪贴板文本","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}},
    {"name":"media_control","description":"发送媒体播放控制","inputSchema":{"type":"object","properties":{"action":{"type":"string","enum":["play_pause","next","previous","stop"]}},"required":["action"]}},
    {"name":"set_volume","description":"设置系统音量 0-100","inputSchema":{"type":"object","properties":{"percent":{"type":"integer","minimum":0,"maximum":100}},"required":["percent"]}},
    {"name":"set_brightness","description":"设置显示器亮度 0-100","inputSchema":{"type":"object","properties":{"percent":{"type":"integer","minimum":0,"maximum":100}},"required":["percent"]}},
    {"name":"type_text","description":"向当前窗口输入文字","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}},
]

def result(value, error=False):
    return {"content":[{"type":"text","text":json.dumps(value,ensure_ascii=False) if not isinstance(value,str) else value}],"isError":error}

def call(name, args):
    try:
        if name == "open_url":
            url=args["url"]
            if urlparse(url).scheme not in {"http","https"}: return result("只允许 HTTP/HTTPS 地址",True)
            return result({"opened":webbrowser.open(url),"url":url})
        if name == "launch_app":
            subprocess.Popen([args["program"],*args.get("args",[])],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            return result({"started":True,"program":args["program"]})
        path=Path(args["path"]).expanduser() if "path" in args else None
        if name == "open_folder":
            subprocess.Popen(["explorer.exe",("/select," if args.get("select") else "")+str(path)],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)); return result({"opened":True,"path":str(path)})
        if name == "delete_file":
            if not path or not path.is_file(): return result("目标不是文件",True)
            if args.get("use_recycle_bin",True):
                from send2trash import send2trash; send2trash(str(path))
            else: path.unlink()
            return result({"deleted":True,"path":str(path)})
        source=Path(args["source"]).expanduser() if "source" in args else None
        dest=Path(args["destination"]).expanduser() if "destination" in args else None
        if name == "copy_file": shutil.copytree(source,dest,dirs_exist_ok=True) if source.is_dir() else shutil.copy2(source,dest); return result({"copied":True})
        if name == "move_file": shutil.move(source,dest); return result({"moved":True})
        if name == "rename_file":
            target=path.with_name(args["new_name"]); path.rename(target); return result({"renamed":True,"path":str(target)})
        if name == "file_info":
            stat=path.stat(); return result({"path":str(path),"is_dir":path.is_dir(),"size":stat.st_size,"modified":stat.st_mtime})
        if name == "create_folder": path.mkdir(parents=True,exist_ok=True); return result({"created":True,"path":str(path)})
        if name == "search_files":
            pattern=str(path / ("**" if args.get("recursive",True) else "") / args["pattern"]); return result({"matches":glob.glob(pattern,recursive=args.get("recursive",True))[:10000]})
        if name == "zip_files":
            destination=Path(args["destination"]).expanduser()
            with zipfile.ZipFile(destination,"w",zipfile.ZIP_DEFLATED) as z:
                for item in args["paths"]:
                    p=Path(item).expanduser(); files=p.rglob("*") if p.is_dir() else [p]
                    for f in files:
                        if f.is_file(): z.write(f,f.relative_to(p.parent))
            return result({"created":True,"path":str(destination)})
        if name == "unzip_file":
            destination=Path(args["destination"]).expanduser(); destination.mkdir(parents=True,exist_ok=True)
            with zipfile.ZipFile(args["path"]) as z:
                root=destination.resolve();
                if any(not (root/entry).resolve().is_relative_to(root) for entry in z.namelist()): return result("压缩包包含越界路径",True)
                z.extractall(destination)
            return result({"extracted":True,"path":str(destination)})
        if name in {"open_mailto","open_tel"}:
            import urllib.parse
            target=("mailto:"+args["address"]+"?"+urllib.parse.urlencode({k:args[k] for k in ("subject","body") if k in args})) if name=="open_mailto" else "tel:"+args["number"]
            webbrowser.open(target); return result({"opened":True,"target":target})
        if name == "get_system_info":
            import platform
            return result({"system":platform.platform(),"python":platform.python_version(),"cpu":os.cpu_count()})
        if name == "get_network_status":
            import socket
            return result({"hostname":socket.gethostname(),"address":socket.gethostbyname(socket.gethostname())})
        if name == "get_battery_status":
            try:
                import psutil
                battery=psutil.sensors_battery()
                return result({"available":battery is not None, "percent":battery.percent if battery else None, "plugged":battery.power_plugged if battery else None})
            except Exception: return result({"available":False})
        if name == "lock_screen":
            import ctypes; ctypes.windll.user32.LockWorkStation(); return result({"locked":True})
        if name == "sleep_computer":
            if os.name == "nt":
                ctypes.windll.kernel32.SetSystemPowerState(False, True)
            return result({"requested":True})
        if name == "shutdown_computer":
            action=args["action"]
            if action=="cancel": subprocess.run(["shutdown","/a"],check=False)
            else: subprocess.Popen(["shutdown","/r" if action=="restart" else "/s","/t",str(args.get("delay_seconds",30))])
            return result({"requested":action})
        if name in {"clipboard_read","clipboard_write"}:
            import tkinter as tk
            root=tk.Tk(); root.withdraw()
            if name == "clipboard_read": value=root.clipboard_get(); root.destroy(); return result(value)
            root.clipboard_clear(); root.clipboard_append(args["text"]); root.update(); root.destroy(); return result({"written":True})
        if name == "media_control":
            import ctypes
            keys={"play_pause":0xB3,"next":0xB0,"previous":0xB1,"stop":0xB2}; ctypes.windll.user32.keybd_event(keys[args["action"]],0,0,0); ctypes.windll.user32.keybd_event(keys[args["action"]],0,2,0); return result({"sent":args["action"]})
        if name == "set_volume":
            try:
                from pycaw.pycaw import AudioUtilities
                endpoint=AudioUtilities.GetSpeakers().EndpointVolume; endpoint.SetMasterVolumeLevelScalar(args["percent"]/100,None); return result({"percent":args["percent"]})
            except Exception as exc: return result(f"音量设置失败：{exc}",True)
        if name == "set_brightness":
            try:
                import screen_brightness_control as sbc
                sbc.set_brightness(args["percent"]); return result({"percent":args["percent"]})
            except Exception as exc: return result(f"亮度设置失败：{exc}",True)
        if name == "type_text":
            import ctypes
            ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p)==8 else ctypes.c_ulong
            class KEYBDINPUT(ctypes.Structure):
                _fields_=[("wVk",ctypes.c_ushort),("wScan",ctypes.c_ushort),("dwFlags",ctypes.c_ulong),("time",ctypes.c_ulong),("dwExtraInfo",ULONG_PTR)]
            class INPUT(ctypes.Structure):
                _fields_=[("type",ctypes.c_ulong),("ki",KEYBDINPUT)]
            inputs=[]
            for ch in args["text"]:
                code=ord(ch)
                units=[code] if code<=0xffff else [0xd800+((code-0x10000)>>10),0xdc00+((code-0x10000)&0x3ff)]
                for unit in units:
                    inputs.extend([INPUT(1,KEYBDINPUT(0,unit,0x0004,0,0)),INPUT(1,KEYBDINPUT(0,unit,0x0004|0x0002,0,0))])
            sent=ctypes.windll.user32.SendInput(len(inputs),(INPUT*len(inputs))(*inputs),ctypes.sizeof(INPUT)) if inputs and os.name=="nt" else 0
            return result({"typed":len(args["text"]),"sent":sent==len(inputs)})
        return result("未知工具",True)
    except Exception as exc: return result(f"执行失败：{exc}",True)

def serve():
    # windowed 冻结程序的 sys.stdin/stdout 为 None，使用父进程匿名管道。
    import io
    input_stream = io.TextIOWrapper(os.fdopen(os.dup(0), "rb"), encoding="utf-8")
    output_stream = io.TextIOWrapper(os.fdopen(os.dup(1), "wb"), encoding="utf-8", write_through=True)
    for line in input_stream:
        request=json.loads(line); method=request.get("method"); params=request.get("params",{})
        if method == "notifications/initialized": continue
        if method == "initialize": response={"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"voicepet","version":"0.1.0"}}
        elif method == "tools/list": response={"tools":TOOLS}
        elif method == "tools/call": response=call(params.get("name",""),params.get("arguments",{}))
        else:
            if "id" in request: print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32601,"message":"方法不支持"}},ensure_ascii=False),file=output_stream,flush=True)
            continue
        if "id" in request: print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":response},ensure_ascii=False),file=output_stream,flush=True)

if __name__ == "__main__": serve()
