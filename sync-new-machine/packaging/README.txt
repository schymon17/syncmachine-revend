ReVend Sync - agent synchronizacji automatu

Instalacja (jako administrator), kod instalacyjny z karty maszyny w panelu:

  Windows 10/11 - PowerShell:
    & ([scriptblock]::Create((irm https://panel.revend.pl/agent/install.ps1))) -Code RV-XXXX-XXXX-XXXX

  Windows 7 albo bez internetu w PowerShellu:
    rozpakuj ten zip na maszynie i uruchom:  install.cmd RV-XXXX-XXXX-XXXX

Instalator sam znajduje i wylacza starego agenta PHP (daemon.bat), przejmuje
jego ustawienia bazy i miejsce, w ktorym skonczyl wysylac.

Po instalacji:
  C:\Program Files\ReVend\Sync      program (versions\<wersja>, launcher.cmd)
  C:\ProgramData\ReVend\Sync        dane, kolejka, logi (logs\agent.log)
  Harmonogram zadan: "ReVend Sync"  start z systemem, restart po awarii

Diagnostyka (w wierszu polecen jako administrator):
  "C:\Program Files\ReVend\Sync\revend-sync.cmd" status
  "C:\Program Files\ReVend\Sync\revend-sync.cmd" check

Odinstalowanie (z przywroceniem starego agenta):
  "C:\Program Files\ReVend\Sync\revend-sync.cmd" uninstall --restore-legacy
