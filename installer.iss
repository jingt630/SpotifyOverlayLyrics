; Inno Setup 6. Compile after build_overlay.ps1.
; Output: dist\SpotifyOverlayLyrics-Setup.exe

#define AppName "Spotify Lyric Overlay"
#define AppExe "SpotifyOverlayLyrics.exe"

[Setup]
AppName={#AppName}
AppVersion=1.0.0
DefaultDirName={autopf}\SpotifyOverlayLyrics
DefaultGroupName={#AppName}
OutputDir=dist
OutputBaseFilename=SpotifyOverlayLyrics-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}

[Files]
Source: "dist\SpotifyOverlayLyrics\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent
