"""VoicePet 本地 MCP stdio 服务。"""
from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import subprocess
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from . import computer_use
from .agent_types import AgentApprovalMode

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
    {"name":"type_text","description":"向当前窗口输入文字，默认用剪贴板粘贴以避免输入法串字，粘贴失败才退回逐字注入；输入后截图确认一次即可","inputSchema":{"type":"object","properties":{"text":{"type":"string"},"method":{"type":"string","enum":["auto","keys","clipboard"]}},"required":["text"]}},
    {"name":"list_windows","description":"列出当前可见的桌面窗口（句柄、标题、进程、位置），用于定位要操作的窗口","inputSchema":{"type":"object","properties":{"include_minimized":{"type":"boolean"},"limit":{"type":"integer","minimum":1,"maximum":200}}}},
    {"name":"get_window_state","description":"截取窗口或整屏图像供查看，坐标以该图像为准；操作前先用它观察界面","inputSchema":{"type":"object","properties":{"window":{"type":"string","description":"窗口标题关键字或句柄，省略或 active 表示当前活动窗口，screen 表示整屏"},"max_width":{"type":"integer","minimum":200,"maximum":3840}}}},
    {"name":"click","description":"点击窗口内坐标，坐标相对窗口左上角（window 为 screen 时是屏幕坐标）；先用 get_window_state 观察","inputSchema":{"type":"object","properties":{"x":{"type":"integer"},"y":{"type":"integer"},"window":{"type":"string"},"button":{"type":"string","enum":["left","right","middle"]},"clicks":{"type":"integer","minimum":1,"maximum":3},"activate":{"type":"boolean"}},"required":["x","y"]}},
    {"name":"scroll","description":"滚动鼠标滚轮，amount 为正向上、负向下","inputSchema":{"type":"object","properties":{"amount":{"type":"integer","minimum":-20,"maximum":20},"x":{"type":"integer"},"y":{"type":"integer"},"window":{"type":"string"}},"required":["amount"]}},
    {"name":"drag","description":"按住鼠标从起点拖到终点，坐标相对窗口左上角","inputSchema":{"type":"object","properties":{"from_x":{"type":"integer"},"from_y":{"type":"integer"},"to_x":{"type":"integer"},"to_y":{"type":"integer"},"window":{"type":"string"},"button":{"type":"string","enum":["left","right","middle"]}},"required":["from_x","from_y","to_x","to_y"]}},
    {"name":"press_key","description":"按下按键或组合键，例如 enter、ctrl+s、alt+F4、win+r","inputSchema":{"type":"object","properties":{"sequence":{"type":"string"}},"required":["sequence"]}},
]

# 直接操作桌面或不可逆的工具只允许在全自动模式下执行
RESTRICTED_TOOLS = frozenset({
    "click","scroll","drag","press_key","type_text",
    "delete_file","move_file","rename_file","lock_screen","sleep_computer","shutdown_computer",
})
MODE_LABELS = {AgentApprovalMode.SUGGEST:"建议模式",AgentApprovalMode.AUTO_EDIT:"自动编辑模式",AgentApprovalMode.FULL_AUTO:"全自动模式"}

def result(value, error=False):
    return {"content":[{"type":"text","text":json.dumps(value,ensure_ascii=False) if not isinstance(value,str) else value}],"isError":error}

def image_result(value, png):
    return {"content":[{"type":"text","text":json.dumps(value,ensure_ascii=False)},{"type":"image","data":base64.b64encode(png).decode("ascii"),"mimeType":"image/png"}],"isError":False}

def integer_argument(args, key, default, minimum, maximum):
    value = args.get(key, default)
    if value is None or isinstance(value, bool): value = default
    try: number = int(value)
    except (TypeError, ValueError): raise computer_use.ComputerUseError(f"参数 {key} 必须是整数")
    return max(minimum, min(maximum, number))

def approval_mode():
    """读取 Agent 写入的共享模式文件；缺失或损坏时按最保守的建议模式处理"""
    raw = os.environ.get("VOICEPET_MODE_FILE")
    if not raw: return AgentApprovalMode.SUGGEST.value
    try: data = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError): return AgentApprovalMode.SUGGEST.value
    mode = data.get("mode") if isinstance(data,dict) and data.get("version") == 1 else None
    return mode if mode in {item.value for item in AgentApprovalMode} else AgentApprovalMode.SUGGEST.value

def call(name, args):
    try:
        mode = approval_mode()
        if name in RESTRICTED_TOOLS and mode != AgentApprovalMode.FULL_AUTO.value:
            return result(f"{MODE_LABELS[AgentApprovalMode(mode)]}下不能执行 {name}，请让用户切换到全自动模式后重试",True)
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
                from send2trash import send2trash
                send2trash(str(path))
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
            except Exception: return result({"available":False})  # noqa: BLE001 电池信息缺失时按不可用处理
        if name == "lock_screen":
            import ctypes
            ctypes.windll.user32.LockWorkStation(); return result({"locked":True})
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
            if name == "clipboard_read":
                text=computer_use.read_clipboard_text()
                return result(text if text is not None else "剪贴板没有文本内容",text is None)
            computer_use.write_clipboard_text(str(args["text"])); return result({"written":True})
        if name == "media_control":
            import ctypes
            keys={"play_pause":0xB3,"next":0xB0,"previous":0xB1,"stop":0xB2}; ctypes.windll.user32.keybd_event(keys[args["action"]],0,0,0); ctypes.windll.user32.keybd_event(keys[args["action"]],0,2,0); return result({"sent":args["action"]})
        if name == "set_volume":
            try:
                from pycaw.pycaw import AudioUtilities
                endpoint=AudioUtilities.GetSpeakers().EndpointVolume; endpoint.SetMasterVolumeLevelScalar(args["percent"]/100,None); return result({"percent":args["percent"]})
            except Exception as exc: return result(f"音量设置失败：{exc}",True)  # noqa: BLE001 音频设备不可用时只回传失败原因
        if name == "set_brightness":
            try:
                import screen_brightness_control as sbc
                sbc.set_brightness(args["percent"]); return result({"percent":args["percent"]})
            except Exception as exc: return result(f"亮度设置失败：{exc}",True)  # noqa: BLE001 亮度接口不可用时只回传失败原因
        if name == "type_text":
            return result(computer_use.type_text(str(args["text"]),method=str(args.get("method","auto"))))
        if name == "list_windows":
            windows=[item.to_mapping() for item in computer_use.list_windows(include_minimized=bool(args.get("include_minimized")),limit=integer_argument(args,"limit",60,1,200))]
            return result({"count":len(windows),"windows":windows})
        if name == "get_window_state":
            handle=computer_use.resolve_window(args.get("window"))
            capture=computer_use.capture_window(handle,max_width=integer_argument(args,"max_width",1400,200,3840))
            return image_result({"window":handle,"width":capture.width,"height":capture.height,"scaled_by":capture.step},capture.png)
        if name == "click":
            return result(computer_use.click(integer_argument(args,"x",0,-100000,100000),integer_argument(args,"y",0,-100000,100000),window=args.get("window"),button=str(args.get("button","left")),clicks=integer_argument(args,"clicks",1,1,3),activate=args.get("activate",True) is not False))
        if name == "scroll":
            return result(computer_use.scroll(amount=integer_argument(args,"amount",0,-20,20),x=args.get("x"),y=args.get("y"),window=args.get("window")))
        if name == "drag":
            return result(computer_use.drag(integer_argument(args,"from_x",0,-100000,100000),integer_argument(args,"from_y",0,-100000,100000),integer_argument(args,"to_x",0,-100000,100000),integer_argument(args,"to_y",0,-100000,100000),window=args.get("window"),button=str(args.get("button","left"))))
        if name == "press_key":
            return result(computer_use.press_key(str(args["sequence"])))
        return result("未知工具",True)
    except Exception as exc: return result(f"执行失败：{exc}",True)  # noqa: BLE001 单个工具失败只回传结果，不中断 MCP 服务

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
