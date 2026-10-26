"""Render only a disposable RAW copy, through the installed Adobe engine."""
import json
import os
import subprocess
from pathlib import Path


def render_raw(source,destination,job,control=None):
    if os.name!='nt':raise ValueError('XMP 渲染目前需要 Windows 上安装的 Photoshop / Camera Raw')
    if control and not control.checkpoint():raise ValueError('调色已取消')
    source,destination,job=Path(source).resolve(),Path(destination).resolve(),Path(job).resolve()
    if not source.is_relative_to(job) or not destination.is_relative_to(job):raise ValueError('渲染仅允许使用本次临时副本')
    if destination.exists():raise ValueError('渲染目标已存在，拒绝覆盖')
    script=job/'render.jsx'
    script.write_text('var inputPath='+json.dumps(str(source))+';var outputPath='+json.dumps(str(destination))+';\n'+'''(function(){
var previousDialogs=app.displayDialogs,previousDocument=null,document=null;
try {
    if(app.documents.length) previousDocument=app.activeDocument;
    app.displayDialogs=DialogModes.NO;
    document=app.open(new File(inputPath));
    if(document.mode!=DocumentMode.RGB) document.changeMode(ChangeMode.RGB);
    document.convertProfile("sRGB IEC61966-2.1",Intent.RELATIVECOLORIMETRIC,true,true);
    document.bitsPerChannel=BitsPerChannelType.EIGHT;
    var options=new PNGSaveOptions();options.compression=1;options.interlaced=false;
    document.saveAs(new File(outputPath),options,true,Extension.LOWERCASE);
} finally {
    try {if(document) document.close(SaveOptions.DONOTSAVECHANGES);}
    finally {
        app.displayDialogs=previousDialogs;
        if(previousDocument) {try {app.activeDocument=previousDocument;}catch(ignore){}}
    }
}
return "BOYO_RENDER_OK";
})();''',encoding='ascii')
    helper=Path(__file__).with_name('assets')/'render_photoshop.ps1'
    command=['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-WindowStyle','Hidden',
             '-ExecutionPolicy','Bypass','-File',str(helper),'-ScriptPath',str(script)]
    try:
        result=subprocess.run(command,cwd=job,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                              timeout=240,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:raise ValueError('Photoshop 本张处理超时，已停止后续处理；不会强制关闭你的 Photoshop')
    message=result.stdout.decode('utf-8',errors='replace')
    if result.returncode or not destination.is_file() or 'BOYO_RENDER_OK' not in message:
        raise ValueError('Photoshop / Camera Raw 渲染失败：'+message[-1200:])
    if control and not control.checkpoint():raise ValueError('调色已取消，当前临时渲染已完成')
