# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    # 资源清单单一事实源注释（R9-F11）：本 spec 与 tools/build_exe.command 的
    # --add-data 是同一份资源清单的两处入口——改 web/ 或 assets/ 的打包内容，
    # 两边必须同步改（web/ 里也不放任何不该对外提供的文件，design.html 已移 docs/）
    datas=[('web', 'web'), ('assets/samples', 'assets/samples')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='suanjian',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
