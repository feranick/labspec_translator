' ---------------------------------------------------------------------------------------------------------------
' discover_main.vbs - run once in LabSpec: lists the motors and their positions, reads the detector, and tests the
' pieces the bridge relies on (file access, the non-blocking window, Pause, the stop button). The laser is not
' used, nothing moves and no LabSpec data is created. Each step is logged in LS6T_HOME\discovery_progress.txt;
' a step that stopped LabSpec is skipped next time (discovery_skip.txt). Version: @@VERSION@@
' ---------------------------------------------------------------------------------------------------------------

Sub DiscoverMain()
    Dim outPath, stamp
    stamp = Year(Now) & Right("0" & Month(Now), 2) & Right("0" & Day(Now), 2) & "_" & _
            Right("0" & Hour(Now), 2) & Right("0" & Minute(Now), 2) & Right("0" & Second(Now), 2)
    EnsureFolder LS6T_HOME_DIR
    outPath = LS6T_HOME_DIR & "\discovery_" & stamp & ".txt"
    DiscoverTo outPath, True
    LabSpec.Message "ls6t discovery written to:" & vbCrLf & outPath & vbCrLf & vbCrLf & _
                    "To make a profile from it:  ls6t profile propose """ & outPath & """" & vbCrLf & _
                    "then edit profile.proposed.json and run:  ls6t profile import profile.proposed.json", LS_MB_OK
End Sub

DiscoverMain
