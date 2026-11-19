' ---------------------------------------------------------------------------------------------------------------
' bridge_main.vbs - the ls6t bridge loop (protocol 1). Version: @@VERSION@@
'
' Watches LS6T_BRIDGE_DIR\inbox for <seq>.cmd files written by ls6t, runs the lowest one, writes LS6T_BRIDGE_DIR\outbox\
' <seq>.res, and rewrites LS6T_BRIDGE_DIR\heartbeat.txt about once a second. Only the verbs below exist: the bridge never
' runs code it receives.
'
' The operator stays in charge:
'   - a small window says ls6t is in control; closing it stops the acquisition or move in progress and HALTS the
'     bridge (it refuses commands until ls6t asks to resume and the operator answers Yes);
'   - LabSpec's own stop button (scripts) ends the bridge;
'   - cancelling an acquisition in LabSpec counts as an operator stop.
' ---------------------------------------------------------------------------------------------------------------

Const POLL_MS = 100

Dim gInbox, gOutbox, gHb, gStopFlag, gHalted, gQuit, gLastBeat, gLastPopup, gPopupOpen, gViews, gStoppedBy
Dim gVerb, gSeq, gLastSeq
gInbox = LS6T_BRIDGE_DIR & "\inbox"
gOutbox = LS6T_BRIDGE_DIR & "\outbox"
gHb = LS6T_BRIDGE_DIR & "\heartbeat.txt"
gStopFlag = LS6T_BRIDGE_DIR & "\stop.flag"
gHalted = False
gQuit = False
gPopupOpen = False
gLastBeat = Timer - 100
gLastPopup = Timer - 100
gVerb = ""
gSeq = ""
gLastSeq = ""
Set gViews = CreateObject("Scripting.Dictionary")

' ---------------------------------------------------------------- heartbeat and operator
Sub Beat(state, force)
    Dim ts
    If Not force And Elapsed(gLastBeat) < 0.8 Then Exit Sub
    gLastBeat = Timer
    On Error Resume Next
    Set ts = fso.CreateTextFile(gHb, True, True)
    ts.WriteLine "state=" & state
    ts.WriteLine "verb=" & gVerb
    ts.WriteLine "seq=" & gSeq
    ts.WriteLine "protocol=" & LS6T_PROTOCOL
    ts.WriteLine "version=" & LS6T_VERSION
    ts.Close
    Err.Clear
    On Error GoTo 0
End Sub

Sub BeatNow()
    If gHalted Then
        Beat "halted", False
    ElseIf gVerb <> "" Then
        Beat "busy", False
    Else
        Beat "idle", False
    End If
End Sub

Sub OpenPopup()
    On Error Resume Next
    LabSpec.Message "ls6t (labspec-translator) is controlling the instrument." & vbCrLf & vbCrLf & _
                    "Close this window to STOP: the acquisition or move in progress stops and ls6t waits." & _
                    vbCrLf & "Use LabSpec's script stop button to end the bridge.", LS_MB_NON_BLOCKING
    gPopupOpen = (Err.Number = 0)
    Err.Clear
    On Error GoTo 0
End Sub

Sub ClosePopup()
    On Error Resume Next
    If gPopupOpen Then LabSpec.Message "#", LS_MB_NON_BLOCKING
    gPopupOpen = False
    Err.Clear
    On Error GoTo 0
End Sub

' "" if nothing; "operator" (popup closed: halt) or "quit" (LabSpec's stop button)
Function OperatorStop()
    Dim r
    OperatorStop = ""
    If Elapsed(gLastPopup) < 0.4 Then Exit Function
    gLastPopup = Timer
    On Error Resume Next
    r = LabSpec.MessageEx("", LS_MB_STOP)
    If Err.Number = 0 Then
        If CStr(r) = "1" Then
            OperatorStop = "quit"
            Exit Function
        End If
    End If
    Err.Clear
    If gPopupOpen Then
        r = LabSpec.Message("", LS_MB_NON_BLOCKING)
        If Err.Number = 0 Then
            If r = 0 Then
                gPopupOpen = False
                OperatorStop = "operator"
            End If
        End If
    End If
    Err.Clear
    On Error GoTo 0
End Function

' "" if the command may go on; "request" (ls6t asked) or "operator". A stop.flag counts only for the command it
' names (seq=...): a flag left from an earlier command is removed.
Function StopRequested()
    Dim o, fl
    StopRequested = ""
    If fso.FileExists(gStopFlag) Then
        On Error Resume Next
        Set fl = ReadKV(gStopFlag)
        fso.DeleteFile gStopFlag, True
        If Err.Number = 0 Then
            If gSeq <> "" And GetP(fl, "seq", "") = gSeq Then StopRequested = "request"
        End If
        Err.Clear
        On Error GoTo 0
        If StopRequested <> "" Then Exit Function
    End If
    o = OperatorStop()
    If o <> "" Then
        gHalted = True
        If o = "quit" Then gQuit = True
        LabSpec.Message "ls6t halted by the operator", LS_MB_STATUS_BAR
        StopRequested = "operator"
    End If
End Function

Sub Fail(res, msg)
    res("ok") = 0
    If res("error") = "" Then res("error") = msg
End Sub

Function ErrText(what)
    If Err.Number <> 0 Then
        ErrText = what & ": " & Err.Description & " (" & Err.Number & ")"
    Else
        ErrText = what
    End If
End Function

' ---------------------------------------------------------------- waiting for motors
Function WaitMotor(motor, moveId, tmo, res)
    Dim t0, st, who
    WaitMotor = True
    t0 = Timer
    On Error Resume Next
    Do
        st = LabSpec.GetMotorStatus(motor, moveId)
        If Err.Number <> 0 Then
            Fail res, ErrText("GetMotorStatus " & motor)
            Err.Clear
            WaitMotor = False
            Exit Do
        End If
        If st = 0 Then Exit Do
        who = StopRequested()
        If who <> "" Then
            If motor <> "" Then LabSpec.MoveMotor motor, 0, "", LS_MOTOR_STOP
            gStoppedBy = who
            WaitMotor = False
            Exit Do
        End If
        If Elapsed(t0) > tmo Then
            If motor <> "" Then LabSpec.MoveMotor motor, 0, "", LS_MOTOR_STOP
            Fail res, "motor " & motor & ": not there after " & tmo & " s"
            WaitMotor = False
            Exit Do
        End If
        BeatNow
        LabSpec.Pause 50
    Loop
    Err.Clear
    On Error GoTo 0
End Function

' ---------------------------------------------------------------- verbs
Sub VPing(cmd, res)
    res("protocol") = LS6T_PROTOCOL
    res("version") = LS6T_VERSION
    res("system") = LSValue(0, "SystemName")
End Sub

Sub VStatus(cmd, res)
    res("system") = LSValue(0, "SystemName")
    res("detector_temp_c") = DetectorTemp()
    res("laser_nm") = MotorValue(GetP(cmd, "m_laser", "Laser"))
    res("grating") = MotorValue(GetP(cmd, "m_grating", "Grating"))
    res("filter") = MotorLabel(GetP(cmd, "m_filter", "Filter"))
    res("objective") = MotorLabel(GetP(cmd, "m_objective", "Objective"))
    res("hole_um") = MotorValue(GetP(cmd, "m_hole", "Hole"))
    res("slit_um") = MotorValue(GetP(cmd, "m_slit", "Slit"))
    res("x_um") = MotorValue(GetP(cmd, "m_x", "X"))
    res("y_um") = MotorValue(GetP(cmd, "m_y", "Y"))
    res("z_um") = MotorValue(GetP(cmd, "m_z", "Z"))
    res("exposure_s") = LSValue(0, "LabSpecExposureTime")
    res("accumulations") = LSValue(0, "LabSpecAccumulation")
    res("shutter_mode") = LSValue(0, "ShutterMode")
End Sub

Sub VMove(cmd, res)
    Dim motor, mode, v, posVal, posName, mc, moveId, tmo
    motor = GetP(cmd, "motor", "")
    mode = GetP(cmd, "mode", "value")
    v = GetP(cmd, "value", "")
    tmo = ParseNum(GetP(cmd, "timeout_s", "600"))
    posVal = 0
    posName = ""
    If mode = "label" Then
        mc = LS_MOTOR_STRING
        posName = v
    ElseIf mode = "index" Then
        mc = LS_MOTOR_INDEX
        posVal = ParseNum(v)
    Else
        mc = LS_MOTOR_VALUE
        posVal = ParseNum(v)
    End If
    If MotorSize(motor) = -1 Then
        Fail res, "motor " & motor & " is not present"
        Exit Sub
    End If
    On Error Resume Next
    moveId = LabSpec.MoveMotor(motor, posVal, posName, mc + LS_MOTOR_NO_WAIT)
    If Err.Number <> 0 Then
        Fail res, ErrText("MoveMotor " & motor)
        Err.Clear
        Exit Sub
    End If
    On Error GoTo 0
    If moveId = -1 Then
        Fail res, "LabSpec refused to move " & motor & " to " & v
        Exit Sub
    End If
    If moveId > 0 Then WaitMotor motor, moveId, tmo, res
    res("motor") = motor
    res("value") = MotorValue(motor)
    res("label") = MotorLabel(motor)
End Sub

Sub VMoveXY(cmd, res)
    Dim x, y, r, mx, my
    x = ParseNum(GetP(cmd, "x_um", ""))
    y = ParseNum(GetP(cmd, "y_um", ""))
    mx = GetP(cmd, "m_x", "X")
    my = GetP(cmd, "m_y", "Y")
    On Error Resume Next
    r = LabSpec.MoveXY(x, y, LS_STAGE_REL)
    If Err.Number <> 0 Then
        Fail res, ErrText("MoveXY")
        Err.Clear
        Exit Sub
    End If
    On Error GoTo 0
    If WaitMotor("", 0, ParseNum(GetP(cmd, "timeout_s", "120")), res) = False Then
        On Error Resume Next
        LabSpec.MoveMotor mx, 0, "", LS_MOTOR_STOP
        LabSpec.MoveMotor my, 0, "", LS_MOTOR_STOP
        Err.Clear
        On Error GoTo 0
    End If
    res("x_um") = MotorValue(mx)
    res("y_um") = MotorValue(my)
End Sub

Sub VAutofocus(cmd, res)
    Dim how, mc, t0, st, who, tmo
    how = GetP(cmd, "how", "laser")
    tmo = ParseNum(GetP(cmd, "timeout_s", "180"))
    mc = LS_LASER_AUTOFOCUS
    If how = "raman" Then mc = LS_RAMAN_AUTOFOCUS
    If how = "video" Then mc = LS_VIDEO_AUTOFOCUS
    On Error Resume Next
    LabSpec.Autofocus mc
    If Err.Number <> 0 Then
        Fail res, ErrText("Autofocus (choosing " & how & ")")
        Err.Clear
        Exit Sub
    End If
    LabSpec.Autofocus LS_START_AUTOFOCUS
    If Err.Number <> 0 Then
        Fail res, ErrText("Autofocus")
        Err.Clear
        Exit Sub
    End If
    t0 = Timer
    Do
        st = LabSpec.Autofocus(LS_GET_AUTOFOCUS_STATUS)
        If Err.Number <> 0 Then
            Fail res, ErrText("Autofocus status")
            Err.Clear
            Exit Do
        End If
        If st = 0 Then Exit Do
        who = StopRequested()
        If who <> "" Then
            LabSpec.Autofocus 2       ' STOP_AUTOFOCUS: restores the previous settings
            gStoppedBy = who
            Exit Do
        End If
        If Elapsed(t0) > tmo Then
            LabSpec.Autofocus 2
            Fail res, "autofocus: not done after " & tmo & " s"
            Exit Do
        End If
        BeatNow
        LabSpec.Pause 100
    Loop
    Err.Clear
    On Error GoTo 0
    res("how") = how
    res("z_um") = MotorValue(GetP(cmd, "m_z", "Z"))
End Sub

' Writes spectrum `id` as '# key=value' lines and x<TAB>y rows; fills points, max_counts, x_unit, y_unit
Function WriteSpectrum(id, outPath, res)
    Dim xy, at, au, info, i, xu, yu, ts, tmp, mx, nc, nr, r, nm, unit
    WriteSpectrum = False
    xu = "nm"
    yu = "cnt"
    On Error Resume Next
    LabSpec.GetValue id, "XYData", xy
    If Err.Number <> 0 Or Not IsArray(xy) Then
        Fail res, ErrText("GetValue XYData")
        Err.Clear
        Exit Function
    End If
    LabSpec.GetValue id, "AxisType", at
    LabSpec.GetValue id, "AxisUnits", au
    If IsArray(at) And IsArray(au) Then
        For i = 0 To UBound(at)
            If CStr(at(i)) = "Spectr" Then xu = CStr(au(i))
            If CStr(at(i)) = "Intens" Then yu = CStr(au(i))
        Next
    End If
    Err.Clear
    LabSpec.GetValue id, "AcqInfo", info
    Err.Clear
    tmp = outPath & ".tmp"
    Set ts = fso.CreateTextFile(tmp, True, True)
    If Err.Number <> 0 Then
        Fail res, ErrText("cannot write " & outPath)
        Err.Clear
        Exit Function
    End If
    ts.WriteLine "# id=" & id
    ts.WriteLine "# x_unit=" & xu
    ts.WriteLine "# y_unit=" & yu
    If IsArray(info) Then
        nc = UBound(info, 1)
        nr = UBound(info, 2)
        If Err.Number = 0 Then
            For r = 0 To nr
                nm = ""
                nm = Replace(Replace(CStr(info(0, r)), "=", "-"), vbCrLf, " ")
                unit = ""
                If nc >= 2 Then unit = " " & CStr(info(2, r))
                ts.WriteLine "# acq." & nm & "=" & Fmt(info(1, r)) & unit
            Next
        End If
        Err.Clear
    End If
    mx = -1E+300
    For i = 0 To UBound(xy, 2)
        ts.WriteLine Fmt(xy(0, i)) & vbTab & Fmt(xy(1, i))
        If xy(1, i) > mx Then mx = xy(1, i)
    Next
    ts.Close
    If fso.FileExists(outPath) Then fso.DeleteFile outPath, True
    fso.MoveFile tmp, outPath
    If Err.Number <> 0 Then
        Fail res, ErrText("cannot write " & outPath)
        Err.Clear
        Exit Function
    End If
    On Error GoTo 0
    res("points") = UBound(xy, 2) + 1
    res("max_counts") = mx
    res("x_unit") = xu
    res("y_unit") = yu
    res("out") = outPath
    WriteSpectrum = True
End Function

Sub VAcquire(cmd, res)
    Dim mode, ex, acc, f, t, outPath, native, sample, id, t0, started, who, tmo, r
    mode = CLng(ParseNum(GetP(cmd, "mode", "10")))
    ex = ParseNum(GetP(cmd, "exposure_s", "1"))
    acc = CLng(ParseNum(GetP(cmd, "accumulations", "1")))
    f = ParseNum(GetP(cmd, "from_nm", "0"))
    t = ParseNum(GetP(cmd, "to_nm", "0"))
    outPath = GetP(cmd, "out", "")
    native = GetP(cmd, "native", "")
    sample = GetP(cmd, "sample", "")
    tmo = ParseNum(GetP(cmd, "timeout_s", "3600"))
    If outPath = "" Then
        Fail res, "acquire needs 'out'"
        Exit Sub
    End If
    If (mode Mod 10) <> LS_ACQ_SPECTRUM And (mode Mod 10) <> 2 Then
        Fail res, "acquire: only spectrum modes (0 or 2 plus options)"
        Exit Sub
    End If
    On Error Resume Next
    If ex = 0 Then
        LabSpec.SetAutoExposure ParseNum(GetP(cmd, "ae_test_s", "0.1")), ParseNum(GetP(cmd, "ae_min_s", "0.1")), _
                                ParseNum(GetP(cmd, "ae_max_s", "10")), ParseNum(GetP(cmd, "ae_target", "30000"))
        If Err.Number <> 0 Then
            Fail res, ErrText("SetAutoExposure")
            Err.Clear
            Exit Sub
        End If
    End If
    ' collect earlier results (and a 'cancelled' state) until LabSpec reports none, so the next ID is ours
    For r = 1 To 50
        id = LabSpec.GetAcqID()
        If Err.Number <> 0 Then Exit For
        If id = -1 Then Exit For
        LabSpec.Pause 20
    Next
    Err.Clear
    LabSpec.Acq mode, ex, acc, f, t
    If Err.Number <> 0 Then
        Fail res, ErrText("Acq")
        Err.Clear
        Exit Sub
    End If
    t0 = Timer
    started = False
    Do
        id = LabSpec.GetAcqID()
        If Err.Number <> 0 Then
            Fail res, ErrText("GetAcqID")
            Err.Clear
            Exit Sub
        End If
        If id > 0 Then Exit Do
        If id = -2 And (started Or Elapsed(t0) > 1) Then      ' cancelled in LabSpec: the operator intervened
            gHalted = True
            gStoppedBy = "operator"
            ClosePopup
            Exit Sub
        End If
        If id = 0 Then started = True
        If id = -1 And Not started And Elapsed(t0) > 15 Then
            Fail res, "the acquisition did not start (LabSpec reports none in progress)"
            Exit Sub
        End If
        If id = -1 And started Then
            Fail res, "the acquisition ended without data"
            Exit Sub
        End If
        who = StopRequested()
        If who <> "" Then
            LabSpec.Acq LS_ACQ_CANCEL, 0, 0, 0, 0
            gStoppedBy = who
            res("cancelled") = 1
            Exit Sub
        End If
        If Elapsed(t0) > tmo Then
            LabSpec.Acq LS_ACQ_CANCEL, 0, 0, 0, 0
            Fail res, "acquisition: no data after " & tmo & " s (cancelled)"
            Exit Sub
        End If
        BeatNow
        LabSpec.Pause 50
    Loop
    Err.Clear
    On Error GoTo 0
    res("id") = id
    If Not WriteSpectrum(id, outPath, res) Then Exit Sub
    On Error Resume Next
    If sample <> "" Then LabSpec.PutDataInfo id, "Custom", "Sample", sample
    LabSpec.PutDataInfo id, "Custom", "Remark", "ls6t " & gSeq
    Err.Clear
    If native <> "" Then
        r = LabSpec.Save(id, native, "")
        res("native_ok") = 0
        If Err.Number = 0 And r = 0 Then res("native_ok") = 1
        Err.Clear
    End If
    On Error GoTo 0
    If ex > 0 Then res("exposure_s") = ex       ' auto exposure: the time used is in the file's acq.* lines
    res("accumulations") = acc
    res("detector_temp_c") = DetectorTemp()
End Sub

Sub VView(cmd, res)
    Dim name, action, kind, id, arr, dataId, mapId, pid, r, p, fv, tv
    Dim vals(1), labels(1), units(1), disp(1)
    name = GetP(cmd, "name", "view")
    action = GetP(cmd, "action", "")
    kind = GetP(cmd, "kind", "map")
    id = CLng(ParseNum(GetP(cmd, "id", "0")))
    vals(0) = ParseNum(GetP(cmd, "x", "0"))
    vals(1) = ParseNum(GetP(cmd, "y", "0"))
    labels(0) = "X"
    labels(1) = "Y"
    units(0) = "mkm"      ' LabSpec's name for micrometres
    units(1) = "mkm"
    disp(0) = 1
    disp(1) = 0
    fv = ParseNum(GetP(cmd, "vfrom", "0"))
    tv = ParseNum(GetP(cmd, "vto", "0"))
    On Error Resume Next
    If action = "create" Then
        If kind = "map" Then
            r = LabSpec.Map(LS_CREATE_MAP, dataId, mapId, id, vals, labels, units, disp, fv, tv)
            If Err.Number = 0 And r < 0 Then Fail res, "LabSpec could not create the map"
            If Err.Number = 0 And r >= 0 Then
                LabSpec.Exec mapId, LS_SHOW_DATA, p
                gViews(name) = Array("map", dataId, mapId)
            End If
        Else
            pid = LabSpec.Profile(LS_CREATE_PROFILE, 0, id, ParseNum(GetP(cmd, "value", "0")), _
                                  GetP(cmd, "unit", "s"), GetP(cmd, "label", "time"))
            If Err.Number = 0 And pid > 0 Then
                LabSpec.Exec pid, LS_SHOW_DATA, p
                gViews(name) = Array("profile", 0, pid)
            ElseIf Err.Number = 0 Then
                Fail res, "LabSpec could not create the profile"
            End If
        End If
    ElseIf Not gViews.Exists(name) Then
        Fail res, "no view " & name
    ElseIf action = "add" Then
        arr = gViews(name)
        If arr(0) = "map" Then
            dataId = arr(1)
            mapId = arr(2)
            r = LabSpec.Map(LS_ADD_TO_MAP, dataId, mapId, id, vals, labels, units, disp, fv, tv)
            gViews(name) = Array("map", dataId, mapId)
            LabSpec.Exec mapId, LS_SHOW_DATA, p
        Else
            LabSpec.Profile LS_ADD_TO_PROFILE, arr(2), id, ParseNum(GetP(cmd, "value", "0")), "", ""
            LabSpec.Exec arr(2), LS_SHOW_DATA, p
        End If
    ElseIf action = "save" Then
        arr = gViews(name)
        r = LabSpec.Save(arr(2), GetP(cmd, "path", ""), "")
        If Err.Number = 0 And r <> 0 Then Fail res, "LabSpec could not save the view"
        res("path") = GetP(cmd, "path", "")
    Else
        Fail res, "view action " & action
    End If
    If Err.Number <> 0 Then Fail res, ErrText("view " & action)
    Err.Clear
    On Error GoTo 0
    res("view") = name
End Sub

Sub VConfirm(cmd, res)
    Dim b
    On Error Resume Next
    b = LabSpec.Message(GetP(cmd, "text", "ls6t: continue?"), LS_MB_YESNO + LS_MB_ICONQUESTION)
    If Err.Number <> 0 Then
        Fail res, ErrText("Message")
        Err.Clear
        Exit Sub
    End If
    On Error GoTo 0
    If b = LS_IDYES Then
        res("answer") = "yes"
    Else
        res("answer") = "no"
    End If
End Sub

Sub VSafe(cmd, res)
    Dim st
    On Error Resume Next
    st = LabSpec.GetMotorStatus("", 0)
    If st <> 0 Then
        LabSpec.MoveMotor GetP(cmd, "m_x", "X"), 0, "", LS_MOTOR_STOP
        LabSpec.MoveMotor GetP(cmd, "m_y", "Y"), 0, "", LS_MOTOR_STOP
        LabSpec.MoveMotor GetP(cmd, "m_z", "Z"), 0, "", LS_MOTOR_STOP
    End If
    If LabSpec.GetAcqID() = 0 Then LabSpec.Acq LS_ACQ_CANCEL, 0, 0, 0, 0
    Err.Clear
    On Error GoTo 0
    res("shutter_mode") = LSValue(0, "ShutterMode")
End Sub

Sub VResume(cmd, res)
    Dim b
    If Not gHalted Then
        res("halted") = 0
        Exit Sub
    End If
    On Error Resume Next
    b = LabSpec.Message("ls6t asks to control the instrument again. Allow it?", LS_MB_YESNO + LS_MB_ICONQUESTION)
    Err.Clear
    On Error GoTo 0
    If b = LS_IDYES Then
        gHalted = False
        OpenPopup
        res("answer") = "yes"
    Else
        res("answer") = "no"
    End If
End Sub

' ---------------------------------------------------------------- one command
Function NextCommand()
    Dim f, best
    best = ""
    On Error Resume Next
    For Each f In fso.GetFolder(gInbox).Files
        If LCase(Right(f.Name, 4)) = ".cmd" Then
            If best = "" Or f.Name < best Then best = f.Name
        End If
    Next
    Err.Clear
    On Error GoTo 0
    If best = "" Then
        NextCommand = ""
    Else
        NextCommand = gInbox & "\" & best
    End If
End Function

Sub HandleFile(path)
    Dim cmd, res, verb, pp
    On Error Resume Next
    Set cmd = ReadKV(path)
    If Err.Number <> 0 Then
        Err.Clear
        Exit Sub
    End If
    fso.DeleteFile path, True
    Err.Clear
    On Error GoTo 0
    verb = GetP(cmd, "verb", "")
    If GetP(cmd, "seq", "0") = gLastSeq Then Exit Sub      ' the .cmd could not be deleted: never run it twice
    gVerb = verb
    gSeq = GetP(cmd, "seq", "0")
    gLastSeq = gSeq
    Set res = CreateObject("Scripting.Dictionary")
    res("seq") = gSeq
    res("ok") = 1
    res("error") = ""
    gStoppedBy = ""
    Beat "busy", True
    If gHalted And InStr(",ping,status,resume,quit,safe,message,", "," & verb & ",") = 0 Then
        Fail res, "halted by the operator: resume in LabSpec"
    Else
        Select Case verb
            Case "ping": VPing cmd, res
            Case "status": VStatus cmd, res
            Case "discover"
                On Error Resume Next
                res("out") = DiscoverTo(GetP(cmd, "out", LS6T_BRIDGE_DIR & "\discovery.txt"), False)
                If Err.Number <> 0 Then Fail res, ErrText("discover")
                Err.Clear
                On Error GoTo 0
            Case "move": VMove cmd, res
            Case "move_xy": VMoveXY cmd, res
            Case "autofocus": VAutofocus cmd, res
            Case "acquire": VAcquire cmd, res
            Case "view": VView cmd, res
            Case "remove"
                On Error Resume Next
                LabSpec.Exec CLng(ParseNum(GetP(cmd, "id", "0"))), 2, pp     ' REMOVE_DATA
                Err.Clear
                On Error GoTo 0
            Case "save"
                On Error Resume Next
                If LabSpec.Save(CLng(ParseNum(GetP(cmd, "id", "0"))), GetP(cmd, "path", ""), "") <> 0 Then
                    Fail res, "LabSpec could not save"
                End If
                If Err.Number <> 0 Then Fail res, ErrText("Save")
                Err.Clear
                On Error GoTo 0
            Case "confirm": VConfirm cmd, res
            Case "message"
                On Error Resume Next
                LabSpec.Message GetP(cmd, "text", ""), LS_MB_STATUS_BAR
                Err.Clear
                On Error GoTo 0
            Case "safe": VSafe cmd, res
            Case "resume": VResume cmd, res
            Case "quit": gQuit = True
            Case Else: Fail res, "unknown verb " & verb
        End Select
    End If
    If gStoppedBy <> "" Then
        res("ok") = 0
        res("stopped_by") = gStoppedBy
        If gStoppedBy = "operator" Then
            res("error") = "stopped by the operator"
        Else
            res("error") = "stopped (stop request)"
        End If
    End If
    If gHalted Then
        res("halted") = 1
    Else
        res("halted") = 0
    End If
    On Error Resume Next
    WriteKV gOutbox & "\" & gSeq & ".res", res
    Err.Clear
    On Error GoTo 0
    gVerb = ""
    BeatNow
End Sub

' ---------------------------------------------------------------- main loop
Sub BridgeMain()
    Dim p, o
    EnsureFolder LS6T_BRIDGE_DIR
    EnsureFolder gInbox
    EnsureFolder gOutbox
    If fso.FileExists(gStopFlag) Then fso.DeleteFile gStopFlag, True
    OpenPopup
    LabSpec.Message "ls6t bridge " & LS6T_VERSION & " running (" & LS6T_BRIDGE_DIR & ")", LS_MB_STATUS_BAR
    Beat "idle", True
    Do While Not gQuit
        BeatNow
        If fso.FileExists(gStopFlag) Then StopRequested       ' between commands a stop.flag is stale: drop it
        o = OperatorStop()      ' between commands: the popup closed (halt) or LabSpec's stop button (quit)
        If o = "quit" Then gQuit = True
        If o = "operator" Then
            gHalted = True
            LabSpec.Message "ls6t halted by the operator: it waits for a resume", LS_MB_STATUS_BAR
        End If
        If gQuit Then Exit Do
        p = NextCommand()
        If p <> "" Then
            HandleFile p
        Else
            LabSpec.Pause POLL_MS
        End If
    Loop
    ClosePopup
    gVerb = ""
    Beat "stopped", True
    LabSpec.Message "ls6t bridge stopped", LS_MB_STATUS_BAR
End Sub

BridgeMain
