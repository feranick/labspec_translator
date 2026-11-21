' ---------------------------------------------------------------------------------------------------------------
' common.vbs - shared by ls6t_bridge.vbs and ls6t_discover.vbs (ls6t bridge install puts them together).
' Version: @@VERSION@@
'
' VBScript inside LabSpec 6 (the LabSpec object is provided by LabSpec). Constants are prefixed LS_ so they never
' clash with names LabSpec may define itself. Numbers go out with a '.' decimal point and come in through ParseNum,
' whatever the Windows locale (VBScript has no Val; CDbl and CStr follow the locale). Files are UTF-16 (FSO's
' Unicode mode) so sample names and units survive any code page; in values, '%' is written %25 and a line end %0A.
' ---------------------------------------------------------------------------------------------------------------

' LabSpec constants (LabSpec 5/6 Automation manual, 2018)
Const LS_ACQ_SPECTRUM = 0
Const LS_ACQ_GET_TEMPERATURE = 4
Const LS_ACQ_CANCEL = 8
Const LS_MOTOR_VALUE = 0
Const LS_MOTOR_STEP = 1
Const LS_MOTOR_INDEX = 2
Const LS_MOTOR_STRING = 3
Const LS_MOTOR_SIZE = 3
Const LS_MOTOR_STOP = 6
Const LS_MOTOR_NO_WAIT = 10
Const LS_MOTOR_INDEXTOVALUE = 10
Const LS_MOTOR_INDEXTOSTRING = 20
' LabSpec 6.7's own constants (macros\constants.vbm): the same reads for any number of positions
Const LS_MOTOR_INDEXTOVALUE100 = 100
Const LS_MOTOR_INDEXTOSTRING200 = 200
Const LS_START_AUTOFOCUS = 0
Const LS_GET_AUTOFOCUS_STATUS = 1
Const LS_LASER_AUTOFOCUS = 10
Const LS_RAMAN_AUTOFOCUS = 11
Const LS_VIDEO_AUTOFOCUS = 12
Const LS_SHOW_DATA = 0
Const LS_CREATE_MAP = 0
Const LS_ADD_TO_MAP = 1
Const LS_CREATE_PROFILE = 0
Const LS_ADD_TO_PROFILE = 1
Const LS_MB_OK = 0
Const LS_MB_YESNO = 2
Const LS_MB_STATUS_BAR = 6
Const LS_MB_NON_BLOCKING = 8
Const LS_MB_ICONQUESTION = 30
Const LS_MB_ICONWARNING = 50
Const LS_IDYES = 1
Const LS_MB_STOP = 14
Const LS_STAGE_REL = 0
Const LS_START_VIDEO = 0
Const LS_STOP_VIDEO = 1
Const LS_GET_VIDEO_ID = 2
Const LS_GET_ACTIVE_CAMERA = 4
Const LS_SET_ACTIVE_CAMERA = 10
Const LS_REMOVE_DATA = 2
Const LS_CLONE_DATA = 3
Const LS_SHUTTER_MANUAL = 1
Const LS_DIALOG_ADD_ITEM = 0
Const LS_DIALOG_SHOW = 1
Const LS_DIALOG_GET_STATUS = 3
Const LS_DIALOG_UPDATE_VAR = 4
Const LS_MB_OKCANCEL = 1
Const LS_MB_ICONINFORMATION = 20
Const LS_ACQ_AUTO_SHOW = 10
Const LS_ACQ_NO_SPIKE_REMOVING = 100
Const LS_ACQ_SPECTRAL_IMAGE = 3
Const LS_START_EXTENDED_VIDEO = 3
Const LS_VT_R4 = 4
Const LS_VT_R8 = 5
Const LS_VT_BSTR = 8
Const LS_INCREMENT_STEP = 1
Const LS_DISABLE_ALL_MOTORS = 3
Const LS_ENABLE_AXIS = 1

Dim fso, gSep, gLastError
Set fso = CreateObject("Scripting.FileSystemObject")
gSep = Mid(CStr(1.5), 2, 1)      ' the locale's decimal separator
gLastError = ""

' ---------------------------------------------------------------- numbers and text
Function ParseNum(s)
    Dim t
    t = Trim(CStr(s))
    If t = "" Then
        ParseNum = 0
        Exit Function
    End If
    If gSep <> "." Then t = Replace(t, ".", gSep)
    On Error Resume Next
    ParseNum = CDbl(t)
    If Err.Number <> 0 Then
        Err.Clear
        ParseNum = 0
    End If
    On Error GoTo 0
End Function

Function Fmt(x)
    Dim s
    On Error Resume Next
    s = CStr(x)
    If Err.Number <> 0 Then
        Err.Clear
        s = ""
    End If
    On Error GoTo 0
    If IsNumeric(x) And gSep <> "." Then s = Replace(s, gSep, ".")
    Fmt = Replace(Replace(Replace(s, "%", "%25"), vbCr, ""), vbLf, "%0A")
End Function

Function Elapsed(t0)
    Dim d
    d = Timer - t0
    If d < 0 Then d = d + 86400
    Elapsed = d
End Function

Function GetP(cmd, k, def)
    If cmd.Exists(k) Then
        GetP = cmd(k)
    Else
        GetP = def
    End If
End Function

' ---------------------------------------------------------------- files
Sub EnsureFolder(p)
    If Not fso.FolderExists(p) Then
        Dim parent
        parent = fso.GetParentFolderName(p)
        If parent <> "" And Not fso.FolderExists(parent) Then EnsureFolder parent
        fso.CreateFolder p
    End If
End Sub

Function ReadKV(path)
    Dim d, ts, text, lines, i, line, k
    Set d = CreateObject("Scripting.Dictionary")
    Set ts = fso.OpenTextFile(path, 1, False, -1)     ' -1: Unicode (ls6t writes UTF-16)
    text = ""
    If Not ts.AtEndOfStream Then text = ts.ReadAll
    ts.Close
    lines = Split(Replace(text, vbCr, ""), vbLf)
    For i = 0 To UBound(lines)
        line = lines(i)
        k = InStr(line, "=")
        If k > 1 Then d(Left(line, k - 1)) = Replace(Replace(Mid(line, k + 1), "%0A", vbCrLf), "%25", "%")
    Next
    Set ReadKV = d
End Function

' Writes key=value lines to path atomically (a .tmp file, then a rename)
Sub WriteKV(path, d)
    Dim tmp, ts, k
    tmp = path & ".tmp"
    Set ts = fso.CreateTextFile(tmp, True, True)
    For Each k In d.Keys
        ts.WriteLine k & "=" & Fmt(d(k))
    Next
    ts.Close
    If fso.FileExists(path) Then fso.DeleteFile path, True
    fso.MoveFile tmp, path
End Sub

' ---------------------------------------------------------------- LabSpec reads (errors become empty values)
Function MotorSize(name)
    On Error Resume Next
    MotorSize = LabSpec.GetMotorPosition(name, LS_MOTOR_SIZE)
    If Err.Number <> 0 Then
        Err.Clear
        MotorSize = -1
    End If
    On Error GoTo 0
End Function

Function MotorValue(name)
    On Error Resume Next
    MotorValue = LabSpec.GetMotorPosition(name, LS_MOTOR_VALUE)
    If Err.Number <> 0 Then
        Err.Clear
        MotorValue = ""
    End If
    On Error GoTo 0
End Function

Function MotorLabel(name)
    Dim idx, e, v
    MotorLabel = ""
    If name = "" Then Exit Function
    On Error Resume Next
    If LabSpec.GetMotorPosition(name, LS_MOTOR_SIZE) > 0 Then
        idx = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEX)
        ' INDEXTOSTRING (20+i) runs into FULL_INDEXTOVALUE (30) past index 9: LabSpec 6.7's 200+i beyond
        If idx >= 0 And idx <= 9 Then
            MotorLabel = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOSTRING + idx)
        ElseIf idx > 9 Then
            MotorLabel = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOSTRING200 + idx)
        End If
    End If
    If Err.Number <> 0 Then
        Err.Clear
        MotorLabel = ""
    End If
    If MotorLabel = "" Then
        ' the manual's other way: GetValueEx "CurrentPositionName:<motor>" (missing from some builds)
        v = LabSpec.GetValueEx(0, "CurrentPositionName:" & name, e, LS_VT_BSTR)
        If Err.Number = 0 And Not IsArray(v) Then MotorLabel = CStr(v)
        Err.Clear
    End If
    On Error GoTo 0
End Function

' An array value of data object `id`: GetValue, else GetValueEx (documented, missing from some builds), else
' GetValueSimple (no by-reference argument). Returns the method that worked, or "".
Function GetArr(id, what, vt, ByRef v)
    Dim e
    GetArr = ""
    On Error Resume Next
    LabSpec.GetValue id, what, v
    If Err.Number = 0 And IsArray(v) Then
        GetArr = "GetValue"
        Exit Function
    End If
    Err.Clear
    v = LabSpec.GetValueEx(id, what, e, vt)
    If Err.Number = 0 And IsArray(v) Then
        GetArr = "GetValueEx"
        Exit Function
    End If
    Err.Clear
    v = LabSpec.GetValueSimple(id, what, 0, vt)
    If Err.Number = 0 And IsArray(v) Then
        GetArr = "GetValueSimple"
        Exit Function
    End If
    Err.Clear
    On Error GoTo 0
End Function

Function LSValue(id, what)
    Dim v
    On Error Resume Next
    LabSpec.GetValue id, what, v
    If Err.Number <> 0 Then
        Err.Clear
        v = ""
    End If
    On Error GoTo 0
    If IsArray(v) Then
        LSValue = ""
    Else
        LSValue = v
    End If
End Function

Function DetectorTemp()
    Dim t
    On Error Resume Next
    t = LabSpec.Acq(LS_ACQ_GET_TEMPERATURE, 0, 0, 0, 0)
    If Err.Number <> 0 Then
        Err.Clear
        t = ""
    End If
    On Error GoTo 0
    If CStr(t) = "-1000" Then t = ""
    DetectorTemp = t
End Function

' The live video's id and LabSpec's camera, without touching LabSpec's video once the bridge has stopped the video
' itself: on the XploRA (2026-11-20, TESTING T3.12) the stop done HORIBA's way returned, but the next status (which
' asked Video(GET_VIDEO_ID) and Video(GET_ACTIVE_CAMERA)) hung inside LabSpec. After such a stop the bridge knows the
' video is off and which camera was selected; it asks LabSpec again only after it has started the video itself
' (the operator starting it by hand ends the bridge anyway).
Dim gVidOff, gVidCam, gVidStopT
gVidOff = False
gVidCam = ""
gVidStopT = 0
Const LS6T_VIDEO_SETTLE_S = 5           ' after the bridge's own stop, before it starts the video again

' Before the bridge starts the video after its own stop: at least LS6T_VIDEO_SETTLE_S since that stop
Sub VidSettle()
    Dim left
    If Not gVidOff Then Exit Sub
    left = LS6T_VIDEO_SETTLE_S - Elapsed(gVidStopT)
    If left > 0 Then LabSpec.Pause CLng(left * 1000)
End Sub

Function VidId()
    VidId = 0
    If gVidOff Then Exit Function
    On Error Resume Next
    VidId = LabSpec.Video(LS_GET_VIDEO_ID)
    If Err.Number <> 0 Then VidId = 0
    Err.Clear
    On Error GoTo 0
End Function

Function VidCam()
    If gVidOff Then
        VidCam = gVidCam
        Exit Function
    End If
    On Error Resume Next
    VidCam = LabSpec.Video(LS_GET_ACTIVE_CAMERA)
    If Err.Number <> 0 Then VidCam = ""
    Err.Clear
    On Error GoTo 0
End Function

Function Bit(b)
    If b Then
        Bit = 1
    Else
        Bit = 0
    End If
End Function

' ---------------------------------------------------------------- discovery (no laser, no movement)
' Discovery only reads: it never creates, writes or removes LabSpec data, and it asks LabSpec only for what the
' profile needs (the motors below, a few values, the detector, the video, the status bar). Each step is logged to
' LS6T_HOME\discovery_progress.txt before it runs ("begin <step>") and after ("end <step>"), the file closed each
' time, so if LabSpec crashes the log names the step it was in. A step begun and never ended goes to
' LS6T_HOME\discovery_skip.txt, and every later discovery skips it (delete that file to try it again).
Dim gDiscProg, gDiscSkip, gDiscSkipped

Sub DiscAppend(path, line)
    Dim ts
    On Error Resume Next
    Set ts = fso.OpenTextFile(path, 8, True, -1)          ' 8: append, created if missing, Unicode
    ts.WriteLine line
    ts.Close
    Err.Clear
    On Error GoTo 0
End Sub

Sub DiscStart()
    Dim ts, line, lastStep, skipPath
    On Error Resume Next
    EnsureFolder LS6T_HOME_DIR
    gDiscProg = LS6T_HOME_DIR & "\discovery_progress.txt"
    skipPath = LS6T_HOME_DIR & "\discovery_skip.txt"
    Set gDiscSkip = CreateObject("Scripting.Dictionary")
    gDiscSkip.CompareMode = 1
    gDiscSkipped = ""
    If fso.FileExists(gDiscProg) Then
        lastStep = ""
        Set ts = fso.OpenTextFile(gDiscProg, 1, False, -1)
        Do While Not ts.AtEndOfStream
            line = ts.ReadLine
            If Left(line, 6) = "begin " Then lastStep = Mid(line, 7)
            If Left(line, 4) = "end " Or line = "finished" Then lastStep = ""
        Loop
        ts.Close
        If lastStep <> "" Then DiscAppend skipPath, lastStep         ' LabSpec ended inside it last time
    End If
    If fso.FileExists(skipPath) Then
        Set ts = fso.OpenTextFile(skipPath, 1, False, -1)
        Do While Not ts.AtEndOfStream
            line = Trim(ts.ReadLine)
            If line <> "" Then gDiscSkip(line) = 1
        Loop
        ts.Close
    End If
    If fso.FileExists(gDiscProg) Then fso.DeleteFile gDiscProg, True
    Err.Clear
    On Error GoTo 0
    DiscAppend gDiscProg, "start " & Now & " " & LS6T_VERSION
End Sub

' True: run the step (logged as begun); False: it crashed LabSpec before, skip it
Function DStep(name)
    If gDiscSkip.Exists(name) Then
        If gDiscSkipped <> "" Then gDiscSkipped = gDiscSkipped & ","
        gDiscSkipped = gDiscSkipped & name
        DStep = False
    Else
        DiscAppend gDiscProg, "begin " & name
        DStep = True
    End If
    On Error Resume Next
    Execute "BeatNow"                   ' in the bridge (its start-up inventory): the heartbeat goes on; elsewhere none
    Err.Clear
    On Error GoTo 0
End Function

Sub DEnd(name)
    DiscAppend gDiscProg, "end " & name
End Sub

Sub DiscMotor(d, name)
    Dim size, n, j
    size = MotorSize(name)
    If size = -1 Then Exit Sub
    d("motor." & name & ".present") = 1
    d("motor." & name & ".size") = size
    On Error Resume Next
    If size > 0 Then
        n = size
        If n > 60 Then n = 60
        For j = 0 To n - 1
            ' 10+i / 20+i (the 2018 manual) overlap past 10 positions: LabSpec 6.7's 100+i / 200+i then
            If size <= 10 Then
                d("motor." & name & ".value." & j) = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOVALUE + j)
                d("motor." & name & ".label." & j) = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOSTRING + j)
            Else
                d("motor." & name & ".value." & j) = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOVALUE100 + j)
                d("motor." & name & ".label." & j) = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOSTRING200 + j)
            End If
            If Err.Number <> 0 Then Err.Clear
        Next
        If size > 60 Then d("motor." & name & ".truncated") = 1
        d("motor." & name & ".current") = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEX)
        d("motor." & name & ".current_label") = MotorLabel(name)
    Else
        d("motor." & name & ".current") = LabSpec.GetMotorPosition(name, LS_MOTOR_VALUE)
    End If
    If Err.Number <> 0 Then
        d("motor." & name & ".error") = Err.Description
        Err.Clear
    End If
    On Error GoTo 0
End Sub

' withPopup: also test the non-blocking window (not from the bridge: it would close the bridge's own window)
Function DiscoverTo(outPath, withPopup)
    Dim d, names, i, name, ret
    DiscStart
    Set d = CreateObject("Scripting.Dictionary")
    d("time") = Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & "T" & _
                Right("0" & Hour(Now), 2) & ":" & Right("0" & Minute(Now), 2) & ":" & Right("0" & Second(Now), 2)
    d("protocol") = LS6T_PROTOCOL
    d("bridge_version") = LS6T_VERSION
    If DStep("values") Then
        d("system") = LSValue(0, "SystemName")
        d("labspec_path") = LSValue(0, "LabSpecPath")
        d("user_level") = LSValue(0, "UserLevel")
        d("exposure_s") = LSValue(0, "LabSpecExposureTime")
        d("accumulations") = LSValue(0, "LabSpecAccumulation")
        d("shutter_mode") = LSValue(0, "ShutterMode")
        DEnd "values"
    End If
    If DStep("detector") Then
        d("detector_temp_c") = DetectorTemp()
        DEnd "detector"
    End If
    d("decimal_separator") = gSep
    ' the motors a profile uses (names seen on HORIBA instruments); nothing that switches the laser or a lamp
    names = Array("Laser", "Grating", "Filter", "Density", "ND", "Attenuator", "Objective", "Objectives", _
                  "Microscope", "Nosepiece", "Hole", "Slit", "Spectro", "Premono", "X", "Y", "Z", "XT", "YT", _
                  "XL", "YL", "Polarizer", "Raman Polarizer", "Laser Polarizer", "DetectorShutter", _
                  "Shutter", "LaserShutter", "Laser Shutter", "Laser shutter", "LaserPol", "RamanPol")
    For i = 0 To UBound(names)
        name = names(i)
        If DStep("motor." & name) Then
            DiscMotor d, name
            DEnd "motor." & name
        End If
    Next
    ' self-tests for the bridge's design (no laser, no motion, no LabSpec data created)
    On Error Resume Next
    If DStep("selftest.pause") Then
        LabSpec.Pause 10
        d("selftest.pause") = Bit(Err.Number = 0)
        Err.Clear
        DEnd "selftest.pause"
    End If
    If withPopup Then
        If DStep("selftest.popup") Then
            ret = LabSpec.Message("ls6t discovery: this window tests the bridge's stop window. It closes by itself.", _
                                  LS_MB_NON_BLOCKING)
            LabSpec.Pause 300
            d("selftest.popup_open") = LabSpec.Message("", LS_MB_NON_BLOCKING)        ' -1 while open
            LabSpec.Message "#", LS_MB_NON_BLOCKING
            LabSpec.Pause 300
            d("selftest.popup_closed") = LabSpec.Message("", LS_MB_NON_BLOCKING)      ' 0 once closed
            d("selftest.popup_error") = Err.Description
            Err.Clear
            DEnd "selftest.popup"
        End If
    End If
    If DStep("selftest.stop_button") Then
        d("selftest.stop_button") = LabSpec.MessageEx("", LS_MB_STOP)
        d("selftest.stop_button_error") = Err.Description
        Err.Clear
        DEnd "selftest.stop_button"
    End If
    If DStep("video") Then
        d("video.active_camera") = VidCam()
        d("video.id_now") = VidId()                                ' > 0 when the live video runs
        d("video.error") = Err.Description
        Err.Clear
        DEnd "video"
    End If
    If DStep("selftest.last_error_info") Then
        ret = LabSpec.GetLastErrorInfo()                            ' not in the 2018 manual; in the type library
        d("selftest.last_error_info") = Bit(Err.Number = 0)
        Err.Clear
        DEnd "selftest.last_error_info"
    End If
    If DStep("selftest.status_bar") Then
        LabSpec.Message "ls6t discovery running", LS_MB_STATUS_BAR
        d("selftest.status_bar") = Bit(Err.Number = 0)
        Err.Clear
        DEnd "selftest.status_bar"
    End If
    On Error GoTo 0
    If gDiscSkipped <> "" Then d("discovery.skipped") = gDiscSkipped
    WriteKV outPath, d
    DiscAppend gDiscProg, "finished"
    DiscoverTo = outPath
End Function
