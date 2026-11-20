' ---------------------------------------------------------------------------------------------------------------
' discover_main.vbs - run once in LabSpec: lists the motors and their positions, reads the detector, and tests the
' pieces the bridge relies on (file access, the non-blocking window, Pause, the stop button). The laser is not
' used and nothing moves. Version: @@VERSION@@
' ---------------------------------------------------------------------------------------------------------------

Sub DiscoverMain()
    Dim outPath, stamp
    stamp = Year(Now) & Right("0" & Month(Now), 2) & Right("0" & Day(Now), 2) & "_" & _
            Right("0" & Hour(Now), 2) & Right("0" & Minute(Now), 2) & Right("0" & Second(Now), 2)
    EnsureFolder LS6T_HOME_DIR
    outPath = LS6T_HOME_DIR & "\discovery_" & stamp & ".txt"
    DiscoverTo outPath, True, True
    LabSpec.Message "ls6t discovery written to:" & vbCrLf & outPath & vbCrLf & vbCrLf & _
                    "On this PC, run:  ls6t profile import """ & outPath & """", LS_MB_OK
End Sub

DiscoverMain
