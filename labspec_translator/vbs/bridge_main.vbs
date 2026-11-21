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
'   - LabSpec's own stop button for scripts, where LabSpec has one, ends the bridge (this XploRA's LabSpec shows
'     none: ls6t ends it with its 'quit' command, `instrument end_bridge`);
'   - cancelling an acquisition in LabSpec counts as an operator stop.
' ---------------------------------------------------------------------------------------------------------------

Const POLL_MS = 100
Dim gNoId                 ' Empty: GetMotorStatus by the motor's name (HORIBA's scripts pass an unset MoveID)
Dim gHaltPopup            ' the window open is the halted one (closing it ends the bridge)
Dim gCmdT0                ' when the bridge took up the current command (Timer)
gHaltPopup = False
Dim gBlocking             ' the motor while a blocking MoveMotor runs (written in the heartbeat beforehand)
Dim gMoveN                ' MoveMotor calls since this bridge started (1: its first; TESTING T3.16, a slow first move)
gMoveN = 0
gBlocking = ""

Dim gInbox, gOutbox, gHb, gStopFlag, gHalted, gQuit, gLastBeat, gLastPopup, gPopupOpen, gViews, gStoppedBy
Dim gVerb, gSeq, gLastSeq, gChunk
Dim gLaserOpen, gLaserT0, gLaserS, gLaserMotor, gLaserOldMode
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
gLaserOpen = False
gLaserS = 0
gLaserMotor = ""
gLaserOldMode = ""
gChunk = 0
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
    ts.WriteLine "blocking=" & gBlocking        ' the motor of a blocking LabSpec move in progress: no beats meanwhile
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

' The ls6t windows (LabSpec's non-blocking message). Text, then "#<title>#<button>", as HORIBA's own scripts
' title theirs ("...#Standard Mapping: ...#OK"): LabSpec's generic title was "Non blocking" (2026-11-20). No "#"
' in the text itself.
Sub OpenPopup()
    OpenMainPopup False
End Sub

Sub OpenMainPopup(resumed)
    Dim head
    head = "ls6t (labspec-translator) is controlling the instrument."
    If resumed Then head = "ls6t RESUMED: you allowed it, it controls the instrument again (nothing else to answer)."
    On Error Resume Next
    LabSpec.Message head & vbCrLf & vbCrLf & _
                    "Halt = the acquisition or move in progress stops and the bridge waits (it keeps running; " & _
                    "ls6t asks you before going on). It takes effect when LabSpec's current call ends (usually " & _
                    "a second or two); a HALTED window then lets you end the bridge." & vbCrLf & vbCrLf & _
                    "Before using LabSpec's live video buttons, end the bridge (ls6t instrument end-bridge): " & _
                    "LabSpec ends a running script when its video is started or stopped." & _
                    "#ls6t: in control#Halt", LS_MB_NON_BLOCKING
    gPopupOpen = (Err.Number = 0)
    gHaltPopup = False
    Err.Clear
    On Error GoTo 0
End Sub

' Halted (or halting): a window that ends the bridge when closed (2026-11-20: Cancel halted the bridge, its window
' went away, and starting ls6t_bridge.vbs again did nothing: LabSpec runs one script at a time). Opened as soon as
' the closed main window is seen, also during a command, so the operator sees at once that Halt was taken.
Sub OpenHaltedPopup()
    On Error Resume Next
    LabSpec.Message "ls6t is HALTED (or halting: a move or acquisition in progress stops when LabSpec's current " & _
                    "call ends). Nothing new moves or is measured. The bridge still runs." & vbCrLf & vbCrLf & _
                    "End bridge = ends it (then start ls6t_bridge.vbs again when you want ls6t back)." & vbCrLf & _
                    "To go on instead: ls6t instrument resume (LabSpec then asks you)." & _
                    "#ls6t: HALTED#End bridge", LS_MB_NON_BLOCKING
    gPopupOpen = (Err.Number = 0)
    gHaltPopup = gPopupOpen
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
                If gHaltPopup Then
                    gHaltPopup = False
                    OperatorStop = "quit"           ' the halted window closed: end the bridge
                Else
                    OperatorStop = "operator"
                End If
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
        If o = "operator" And Not gPopupOpen Then OpenHaltedPopup     ' at once: Halt was taken
        StopRequested = "operator"
    End If
End Function

Sub Fail(res, msg)
    res("ok") = 0
    If res("error") = "" Then res("error") = msg
End Sub

Function ErrText(what)
    Dim t
    If Err.Number <> 0 Then
        t = what & ": " & Err.Description & " (" & Err.Number & ")"
    Else
        t = what
    End If
    ErrText = t & LabSpecErrorInfo()
End Function

' LabSpec's own last error (GetLastErrorInfo: in the ActiveX type library, not in the 2018 manual), if it has one
Function LabSpecErrorInfo()
    Dim s
    s = LabSpecError()
    LabSpecErrorInfo = ""
    If s <> "" Then LabSpecErrorInfo = " [LabSpec: " & s & "]"
End Function

' The same, bare ("" when LabSpec reports none)
Function LabSpecError()
    Dim v, i, s
    LabSpecError = ""
    On Error Resume Next
    v = LabSpec.GetLastErrorInfo()
    If Err.Number <> 0 Then
        Err.Clear
        Exit Function
    End If
    s = ""
    If IsArray(v) Then
        For i = LBound(v) To UBound(v)
            s = s & " " & CStr(v(i))
        Next
    Else
        s = CStr(v)
    End If
    Err.Clear
    If Trim(s) <> "0" Then LabSpecError = Trim(s)
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
            If motor = "" Then
                Fail res, "the motors were still moving after " & tmo & " s"
            Else
                Fail res, "motor " & motor & ": not there after " & tmo & " s"
            End If
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
    res("warmup") = gWarm                     ' each motor's first status at start (s): TESTING T3.16
    res("system") = LSValue(0, "SystemName")
End Sub

Sub VStatus(cmd, res)
    Dim v
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
    res("last_error") = LabSpecError()
    If GetP(cmd, "m_laser_pol", "") <> "" Then res("laser_pol") = MotorLabel(GetP(cmd, "m_laser_pol", ""))
    If GetP(cmd, "m_raman_pol", "") <> "" Then res("raman_pol") = MotorLabel(GetP(cmd, "m_raman_pol", ""))
    res("laser_open") = Bit(gLaserOpen)
    If gLaserOpen Then res("laser_left_s") = Fmt(gLaserS - Elapsed(gLaserT0))
    On Error Resume Next
    v = VidId()
    res("live_video") = Bit(v > 0)
    res("camera") = VidCam()
    res("video_known_off") = Bit(gVidOff)       ' after the bridge's own stop: not asked of LabSpec
    Err.Clear
    On Error GoTo 0
End Sub

' LabSpec's spectrometer calibration per grating, read with OperVar under the names HORIBA's own service scripts
' use (ACT_INSTRUMENT[<name>][<grating index>]): AF, BF, Diode, Focal, Angle, Gamma, SpMin, SpMax. Read only.
Sub VCalibParams(cmd, res)
    Dim names, n, i, j, v, g
    names = Array("AF", "BF", "Diode", "Focal", "Angle", "Gamma", "SpMin", "SpMax")
    g = GetP(cmd, "m_grating", "Grating")
    n = MotorSize(g)
    If n < 1 Then n = 1
    If n > 10 Then n = 10
    res("gratings") = n
    On Error Resume Next
    res("grating_index") = LabSpec.GetMotorPosition(g, LS_MOTOR_INDEX)
    Err.Clear
    For i = 0 To n - 1
        For j = 0 To UBound(names)
            v = Empty
            LabSpec.OperVar 0, "ACT_INSTRUMENT[" & names(j) & "][" & i & "]", v, 0
            If Err.Number = 0 Then
                If IsNumeric(v) And Not IsEmpty(v) Then
                    res("g" & i & "." & names(j)) = Fmt(v)
                Else
                    res("g" & i & "." & names(j)) = CStr(v)
                End If
            Else
                res("g" & i & "." & names(j) & ".error") = Err.Description
            End If
            Err.Clear
        Next
    Next
    On Error GoTo 0
End Sub

' One of LabSpec's own scripts (its AutoCalibration), run inside the bridge the way HORIBA's scripts include each
' other (ExecuteGlobal). Experimental: only for a .vbs in LabSpec's folder, which ls6t asks for when the profile
' allows it (calibration.autocal) and the operator said Yes. The heartbeat stops while it runs.
Sub VRunVbs(cmd, res)
    Dim path, code, t0, lsPath
    path = GetP(cmd, "path", "")
    lsPath = LCase(LSValue(0, "LabSpecPath"))
    If path = "" Or Not fso.FileExists(path) Then
        Fail res, "run_vbs: no such script: " & path
        Exit Sub
    End If
    If LCase(Right(path, 4)) <> ".vbs" Or lsPath = "" Or InStr(LCase(path), lsPath) <> 1 Then
        Fail res, "run_vbs: only a .vbs inside LabSpec's folder (" & lsPath & ")"
        Exit Sub
    End If
    LabSpec.Message "ls6t: running LabSpec's script " & path, LS_MB_STATUS_BAR
    t0 = Timer
    On Error Resume Next
    code = fso.OpenTextFile(path, 1).ReadAll
    If Err.Number <> 0 Then
        Fail res, ErrText("reading " & path)
        Err.Clear
        Exit Sub
    End If
    ExecuteGlobal code
    If Err.Number <> 0 Then
        Fail res, ErrText("running " & path)
        Err.Clear
    End If
    On Error GoTo 0
    res("seconds") = Fmt(Elapsed(t0))
    res("path") = path
    BeatNow
End Sub

' A motor's named positions (labels and values, up to 60) and the current one
Sub VLabels(cmd, res)
    Dim motor, n, i
    motor = GetP(cmd, "motor", "")
    n = MotorSize(motor)
    If n = -1 Then
        Fail res, "motor " & motor & " is not present"
        Exit Sub
    End If
    res("size") = n
    If n > 60 Then n = 60
    On Error Resume Next
    For i = 0 To n - 1
        If res("size") <= 10 Then
            res("label." & i) = LabSpec.GetMotorPosition(motor, LS_MOTOR_INDEXTOSTRING + i)
            res("value." & i) = LabSpec.GetMotorPosition(motor, LS_MOTOR_INDEXTOVALUE + i)
        Else
            res("label." & i) = LabSpec.GetMotorPosition(motor, LS_MOTOR_INDEXTOSTRING200 + i)
            res("value." & i) = LabSpec.GetMotorPosition(motor, LS_MOTOR_INDEXTOVALUE100 + i)
        End If
        Err.Clear
    Next
    res("current") = MotorLabel(motor)
    Err.Clear
    On Error GoTo 0
End Sub

' The camera's live view in LabSpec: on, off, or its state (camera: the active camera's number, optional)
' The camera. On this XploRA (2026-11-20) ls6t cannot switch cameras reliably: what LabSpec reports
' (Video(GET_ACTIVE_CAMERA)) is the camera selected, not the one shown; a switch to camera 0 was ignored; a switch to
' camera 1 showed only from the next video start; a script's Video(STOP_VIDEO) hung the bridge (twice); and stopping
' or starting the video in LabSpec's interface ends the bridge script. So the bridge never stops the video, and
' switches cameras only where the profile allows it (video.switch_camera, for another instrument, with the video
' stopped); otherwise a camera other than the active one is refused with what the operator does.
Const LS6T_VIDEO_BY_HAND = "end the bridge from ls6t first (instrument end_bridge: LabSpec ends a running script when its video is stopped or started in its interface), then in LabSpec stop the video, choose the camera and start the video, then start ls6t_bridge.vbs again"

' Video(10 + id) with the video stopped, then the selection read back (polled up to 2 s). True when it is `want`.
' Selects camera `want` with the video stopped: after the settle time, Video(10 + want), and nothing read back
' (a video query right after a stop hung LabSpec, T3.12). The caller then starts the video and checks the camera
' while it runs (CameraCheck): the selection only shows from a video start.
Sub SelectCamera(want, res)
    Dim r
    VidSettle
    On Error Resume Next
    r = LabSpec.Video(LS_SET_ACTIVE_CAMERA + CLng(want))
    res("camera_set_returned") = r
    res("camera_set_error") = Err.Description
    Err.Clear
    On Error GoTo 0
    res("camera_asked") = want
    gVidCam = want                          ' for status while the video stays off (until it starts)
End Sub

' After the video was started following SelectCamera: "" when LabSpec runs on the camera asked, else the error
' (camera 0 was ignored on the XploRA before: TESTING Q44).
Function CameraCheck(res)
    Dim now
    CameraCheck = ""
    If CStr(res("camera_asked")) = "" Then Exit Function
    now = VidCam()
    res("camera_after") = now
    If CStr(now) <> CStr(res("camera_asked")) Then
        CameraCheck = "LabSpec ignored the switch: asked camera " & res("camera_asked") & " (Video(" & _
                      (LS_SET_ACTIVE_CAMERA + CLng(res("camera_asked"))) & ") returned " & res("camera_set_returned") & _
                      "), the video restarted on camera " & now & ". To use camera " & res("camera_asked") & ": " & _
                      LS6T_VIDEO_BY_HAND
    End If
End Function

' The checks before a camera is used: "" when ok, else the error. With switching allowed (video.switch_camera) it
' stops a running video HORIBA's way (when video.stop_from_script allows it; res("camera_restart") = 1), selects the
' camera, and leaves the start (and CameraCheck) to the caller.
Function CameraFor(cam, live, allowSwitch, canStop, res)
    Dim cur
    CameraFor = ""
    If cam = "" Then Exit Function
    On Error Resume Next
    cur = VidCam()
    Err.Clear
    On Error GoTo 0
    res("camera_before") = cur
    If CStr(cur) = CStr(CLng(ParseNum(cam))) Then Exit Function
    If Not allowSwitch Then
        CameraFor = "LabSpec's camera is " & cur & ", not " & cam & ", and ls6t does not switch cameras on this " & _
                    "instrument unless the profile allows it (video.switch_camera). To use camera " & cam & ": " & _
                    LS6T_VIDEO_BY_HAND
        Exit Function
    End If
    If live Then
        If Not canStop Then
            CameraFor = "the live video runs on camera " & cur & ": LabSpec switches cameras only when the video " & _
                        "starts, and ls6t may not stop it (video.stop_from_script). To use camera " & cam & ": " & _
                        LS6T_VIDEO_BY_HAND
            Exit Function
        End If
        If Not StopVideoHoriba(res) Then
            CameraFor = "LabSpec's video did not report stopped within 15 s (GetMotorStatus Video = " & _
                        res("video_stop_status") & "): camera not switched"
            Exit Function
        End If
        res("camera_restart") = 1
    End If
    SelectCamera CLng(ParseNum(cam)), res
End Function

' Stopping the live video the way HORIBA's own service scripts do (StandardMapping / SWIFTMapping XploRA.vbs):
' Video STOP_VIDEO as a statement, then GetMotorStatus("Video", id) polled until 0, and no other Video() call in
' between. ls6t's earlier stop (then Pause and Video(GET_VIDEO_ID)) hung twice on the XploRA (2026-11-20); this one
' runs only when the profile allows it (video.stop_from_script), for a test with the maintainer (TESTING T3.12).
Function StopVideoHoriba(res)
    Dim st, mid, t0
    StopVideoHoriba = False
    On Error Resume Next
    gVidCam = LabSpec.Video(LS_GET_ACTIVE_CAMERA)      ' while it runs (as status always did); not asked again after
    Err.Clear
    LabSpec.Video LS_STOP_VIDEO
    If Err.Number <> 0 Then
        res("video_stop_error") = Err.Description
        Err.Clear
        Exit Function
    End If
    t0 = Timer
    Do
        st = LabSpec.GetMotorStatus("Video", mid)
        If Err.Number <> 0 Then
            res("video_stop_status_error") = Err.Description
            Err.Clear
            Exit Do
        End If
        BeatNow
        If st = 0 Or Elapsed(t0) > 15 Then Exit Do
    Loop
    res("video_stop_status") = st
    res("video_stop_s") = Fmt(Elapsed(t0))
    On Error GoTo 0
    StopVideoHoriba = (CStr(st) = "0")
    If StopVideoHoriba Then
        gVidOff = True
        gVidStopT = Timer
    End If
End Function

Sub VCamera(cmd, res)
    Dim action, cam, vid, t0, live0, e
    action = GetP(cmd, "action", "status")
    cam = GetP(cmd, "camera", "")
    If action <> "on" And action <> "off" And action <> "status" Then
        Fail res, "camera action: on, off or status"
        Exit Sub
    End If
    On Error Resume Next
    vid = VidId()
    live0 = (vid > 0)
    res("live") = Bit(live0)
    If action = "off" And live0 Then
        If GetP(cmd, "stop_video", "0") = "1" Then
            If StopVideoHoriba(res) Then
                res("live") = 0
                res("video_id") = 0
            Else
                Fail res, "LabSpec's video did not report stopped within 15 s (GetMotorStatus Video = " & _
                          res("video_stop_status") & ")"
            End If
            Exit Sub
        End If
        res("camera") = VidCam()
        Err.Clear
        Fail res, "ls6t does not stop LabSpec's live video (a script's Video(STOP_VIDEO) hung the bridge on this " & _
                  "instrument): end the bridge from ls6t first (instrument end_bridge), then stop the video in LabSpec"
        Exit Sub
    End If
    If action = "on" Then
        e = CameraFor(cam, live0, GetP(cmd, "switch_camera", "0") = "1", GetP(cmd, "stop_video", "0") = "1", res)
        If e <> "" Then
            res("camera") = VidCam()
            Err.Clear
            Fail res, e
            Exit Sub
        End If
        If CStr(res("camera_restart")) = "1" Then live0 = False      ' stopped for the switch: started below
    ElseIf action = "status" And cam <> "" Then
        If CStr(VidCam()) <> CStr(CLng(ParseNum(cam))) Then
            Fail res, "LabSpec's camera is " & VidCam() & ", not " & cam
            Exit Sub
        End If
    End If
    If action = "on" And Not live0 Then
        VidSettle
        LabSpec.Video LS_START_VIDEO
        If Err.Number <> 0 Then
            Fail res, ErrText("Video start")
            Err.Clear
            Exit Sub
        End If
        gVidOff = False                 ' started by the bridge: LabSpec is asked again (GET_VIDEO_ID, as HORIBA)
        t0 = Timer
        Do
            LabSpec.Pause 100
            vid = LabSpec.Video(LS_GET_VIDEO_ID)
            If Err.Number <> 0 Then
                vid = 0
                Err.Clear
            End If
            If vid > 0 Or Elapsed(t0) > 5 Then Exit Do
        Loop
        e = CameraCheck(res)
        If e <> "" Then
            res("live") = Bit(vid > 0)
            res("camera") = res("camera_after")
            Fail res, e
            Exit Sub
        End If
    End If
    res("live") = Bit(vid > 0)
    res("video_id") = vid
    res("camera") = VidCam()     ' the selection; the view follows it from a video start
    res("video_started") = Bit(action = "on" And Not live0)
    Err.Clear
    On Error GoTo 0
End Sub

' LabSpec's configuration as a file: SaveConfig / LoadConfig (in the type library, not in the 2018 manual)
Sub VConfig(cmd, res)
    Dim action, path, show, r
    action = GetP(cmd, "action", "")
    path = GetP(cmd, "path", "")
    show = CLng(ParseNum(GetP(cmd, "show", "0")))
    If path = "" Then
        Fail res, "config needs 'path'"
        Exit Sub
    End If
    On Error Resume Next
    If action = "save" Then
        r = LabSpec.SaveConfig(path, show)
    ElseIf action = "load" Then
        r = LabSpec.LoadConfig(path, show)
    Else
        Fail res, "config action: save or load"
        Exit Sub
    End If
    If Err.Number <> 0 Then
        Fail res, ErrText(action & "Config")
        Err.Clear
        Exit Sub
    End If
    On Error GoTo 0
    res("returned") = r
    res("path") = path
    res("exists") = Bit(fso.FileExists(path))
End Sub

Sub VMove(cmd, res)
    Dim motor, mode, v, posVal, posName, mc, moveId, tmo, waited, t0, t1
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
    t0 = Timer
    ' blocking=1: MoveMotor without NO_WAIT, as HORIBA's scripts move the grating (MoveMotor("Grating", g, "",
    ' MOTOR_VALUE), then GetMotorStatus with the id it returns): with NO_WAIT the XploRA's grating reported idle at
    ' once, and once stopped at 1800 on its way from 1200 to 2400 (2026-11-20). No stop request while it runs.
    If GetP(cmd, "blocking", "0") = "1" Then
        gBlocking = motor
        Beat "busy", True                   ' says so before LabSpec holds the script: ls6t waits longer, not 'stuck'
        moveId = LabSpec.MoveMotor(motor, posVal, posName, mc)
        gBlocking = ""
        Beat "busy", True
        res("blocking") = 1
    Else
        moveId = LabSpec.MoveMotor(motor, posVal, posName, mc + LS_MOTOR_NO_WAIT)
    End If
    res("move_call_s") = Fmt(Elapsed(t0))           ' how long MoveMotor itself took to return
    gMoveN = gMoveN + 1
    res("bridge_move_n") = gMoveN
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
    res("move_id") = moveId
    ' wait for the motor whatever MoveMotor returned: on the XploRA it gave no move id, so the read-back came before
    ' the motor had moved (2026-11-20). Without an id, GetMotorStatus by the motor's name, as HORIBA's scripts do.
    t1 = Timer
    If moveId > 0 Then
        waited = WaitMotor(motor, moveId, tmo, res)
    Else
        waited = WaitMotor(motor, gNoId, tmo, res)
    End If
    res("wait_s") = Fmt(Elapsed(t1))
    ' and every motor (the manual's WaitForAllMotors: GetMotorStatus("", 0)): a grating change also moves the turret
    ' and the spectrometer; on the XploRA the grating reported idle and its new value 0.2 s after the command while it
    ' was still visibly moving (2026-11-20, T3.15)
    If waited Then
        t1 = Timer
        waited = WaitMotor("", 0, tmo, res)
        res("all_wait_s") = Fmt(Elapsed(t1))
    End If
    ' then the position, read until it is the one asked: a motor passing through other positions (the pinhole read
    ' 100 on its way from 500 to 300) or reporting idle early (the slit is the slowest) is not taken as arrived
    If waited And mode <> "index" Then SettleReadback motor, mode, v, tmo, res
    res("motor") = motor
    res("value") = MotorValue(motor)
    res("label") = MotorLabel(motor)
End Sub

' A label as compared: no spaces, lower case ("10 %" = "10%", "X100_VIS" = "x100_vis")
Function NormLabel(x)
    NormLabel = LCase(Replace(Replace(Trim(CStr(x)), " ", ""), vbTab, ""))
End Function

' Reads the motor until it shows the position asked: a value within 0.5 (637.8 = 637.75; numbers, not their text),
' or the same label as NormLabel compares them, or a label whose number matches. It stops waiting at most 30 s (or
' tmo) after the motor reported idle, or as soon as the reading has not changed for 1.5 s (settle_stable): a
' position written differently is then reported at once, and Python decides whether it matches.
Sub SettleReadback(motor, mode, v, tmo, res)
    Dim t0, got, lim, who, last, tSame, hit
    t0 = Timer
    lim = 30
    If tmo < lim Then lim = tmo
    last = Chr(0)
    tSame = Timer
    Do
        hit = False
        If mode = "label" Then
            got = MotorLabel(motor)
            If NormLabel(got) = NormLabel(v) Then hit = True
        Else
            got = MotorValue(motor)
            If CStr(got) <> "" Then
                If Abs(ParseNum(got) - ParseNum(v)) <= 0.5 Then hit = True
            End If
        End If
        If hit Then Exit Do
        If CStr(got) <> CStr(last) Then
            last = got
            tSame = Timer
        ElseIf Elapsed(tSame) >= 1.5 Then
            res("settle_stable") = 1        ' not the position asked, but no longer moving
            Exit Do
        End If
        who = StopRequested()
        If who <> "" Then
            gStoppedBy = who
            Exit Do
        End If
        If Elapsed(t0) > lim Then
            res("settle_timeout") = 1
            Exit Do
        End If
        BeatNow
        LabSpec.Pause 200
    Loop
    res("settle_s") = Fmt(Elapsed(t0))
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
    If how = "video" And gVidOff Then
        Fail res, "the bridge stopped the live video: start it first (instrument camera_on), then autofocus on video"
        Exit Sub
    End If
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

' With laser_off=1 the acquisition runs with the laser shutter (m_laser_shutter) closed in manual shutter mode, for
' a white-light background; the shutter mode is restored afterwards on every path.
Sub VAcquire(cmd, res)
    Dim shutter, oldMode, newMode
    If GetP(cmd, "laser_off", "0") <> "1" Then
        VAcquireInner cmd, res
        Exit Sub
    End If
    shutter = GetP(cmd, "m_laser_shutter", "")
    If shutter = "" Then
        Fail res, "laser_off needs the laser shutter's motor name"
        Exit Sub
    End If
    oldMode = LSValue(0, "ShutterMode")
    On Error Resume Next
    newMode = LS_SHUTTER_MANUAL
    LabSpec.PutValue 0, "ShutterMode", newMode
    LabSpec.MoveMotor shutter, 0, "", LS_MOTOR_VALUE
    If Err.Number <> 0 Then
        Fail res, ErrText("closing the laser shutter")
        Err.Clear
        newMode = oldMode
        LabSpec.PutValue 0, "ShutterMode", newMode
        Err.Clear
        Exit Sub
    End If
    On Error GoTo 0
    VAcquireInner cmd, res
    On Error Resume Next
    If CStr(oldMode) <> "" Then
        newMode = oldMode
        LabSpec.PutValue 0, "ShutterMode", newMode
    End If
    Err.Clear
    On Error GoTo 0
    res("laser_off") = 1
End Sub

Sub VAcquireInner(cmd, res)
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

' Writes image `id` (a video frame) as '# key=value' header lines (axis types, sizes, units and values), then its
' pixel values, comma-separated, 256 per line, in LabSpec's order. Python decodes and draws it.
Function WriteImage(id, outPath, res)
    WriteImage = WriteData(id, outPath, res, "video")
End Function

' Writes data object `id` (a video frame, a map...) as '# key=value' header lines (axis types, sizes, units, values)
' and its values in LabSpec's order, comma-separated, 256 per line.
Function WriteData(id, outPath, res, kind)
    Dim data, at, sz, au, ax, ts, tmp, i, j, n, line, d2, n1, n2
    Dim total, chunk, start, part, cnt, k, via
    WriteData = False
    On Error Resume Next
    GetArr id, "AxisType", LS_VT_BSTR, at
    GetArr id, "AxisSize", LS_VT_R8, sz
    GetArr id, "AxisUnits", LS_VT_BSTR, au
    GetArr id, "Axis", LS_VT_R8, ax
    Err.Clear
    ' maps: the values in chunks (GetValueSimplePart, in the type library), so a large map is never one array
    chunk = 0
    If kind = "map" Then chunk = CLng(gChunk)
    total = 0
    If chunk > 0 And IsArray(sz) Then
        total = 1
        For i = 0 To UBound(sz)
            If CLng(sz(i)) > 0 Then total = total * CLng(sz(i))
        Next
        ' two values asked must give two values, else the call counts something else (bytes?): read it whole
        part = LabSpec.GetValueSimplePart(id, "Data", 0, LS_VT_R4, 0, 2)
        If Err.Number <> 0 Or Not IsArray(part) Then
            chunk = 0
            Err.Clear
        ElseIf UBound(part) - LBound(part) + 1 <> 2 Then
            chunk = 0
            res("chunk_note") = "GetValueSimplePart gave " & (UBound(part) - LBound(part) + 1) & " values for 2"
        End If
    End If
    If chunk = 0 Then
        via = GetArr(id, "Data", LS_VT_R4, data)
        If via = "" Then
            Fail res, ErrText("GetValue Data (" & kind & ")")
            Err.Clear
            Exit Function
        End If
        res("data_via") = via
    Else
        res("data_via") = "GetValueSimplePart"
    End If
    tmp = outPath & ".tmp"
    Set ts = fso.CreateTextFile(tmp, True, True)
    If Err.Number <> 0 Then
        Fail res, ErrText("cannot write " & outPath)
        Err.Clear
        Exit Function
    End If
    ts.WriteLine "# kind=" & kind
    ts.WriteLine "# id=" & id
    If IsArray(at) Then
        line = ""
        For i = 0 To UBound(at)
            If i > 0 Then line = line & ","
            line = line & CStr(at(i))
        Next
        ts.WriteLine "# axis_types=" & line
    End If
    If IsArray(sz) Then
        line = ""
        For i = 0 To UBound(sz)
            If i > 0 Then line = line & ","
            line = line & Fmt(sz(i))
        Next
        ts.WriteLine "# axis_sizes=" & line
    End If
    If IsArray(au) Then
        line = ""
        For i = 0 To UBound(au)
            If i > 0 Then line = line & ","
            line = line & CStr(au(i))
        Next
        ts.WriteLine "# axis_units=" & line
    End If
    If IsArray(ax) Then
        line = ""
        For i = 0 To UBound(ax)
            If i > 0 Then line = line & ","
            line = line & Fmt(ax(i))
        Next
        ts.WriteLine "# axis_values=" & line
    End If
    If chunk > 0 Then
        ts.WriteLine "# data_dims=" & total
        start = 0
        Do While start < total
            cnt = chunk
            If start + cnt > total Then cnt = total - start
            part = LabSpec.GetValueSimplePart(id, "Data", 0, LS_VT_R4, start, cnt)
            If Err.Number <> 0 Or Not IsArray(part) Then
                Fail res, ErrText("GetValueSimplePart at " & start)
                Err.Clear
                ts.Close
                Exit Function
            End If
            If UBound(part) - LBound(part) + 1 <> cnt Then
                Fail res, "GetValueSimplePart gave " & (UBound(part) - LBound(part) + 1) & " values for " & cnt & _
                          " at " & start & ": set mapping.export_chunk to 0"
                ts.Close
                Exit Function
            End If
            line = ""
            k = 0
            For j = LBound(part) To UBound(part)
                If line <> "" Then line = line & ","
                line = line & Fmt(part(j))
                k = k + 1
                If k Mod 256 = 0 Then
                    ts.WriteLine line
                    line = ""
                End If
            Next
            If line <> "" Then ts.WriteLine line
            start = start + cnt
            BeatNow
        Loop
        ts.Close
        If fso.FileExists(outPath) Then fso.DeleteFile outPath, True
        fso.MoveFile tmp, outPath
        Err.Clear
        On Error GoTo 0
        res("pixels") = total
        WriteData = True
        Exit Function
    End If
    ' the pixel values: one or two dimensions
    n2 = -1
    n1 = UBound(data, 1)
    n2 = UBound(data, 2)
    If Err.Number <> 0 Then
        Err.Clear
        n2 = -1
    End If
    If n2 >= 0 Then
        ts.WriteLine "# data_dims=" & (n1 + 1) & "," & (n2 + 1)
        For j = 0 To n2
            line = ""
            For i = 0 To n1
                If i > 0 Then line = line & ","
                line = line & Fmt(data(i, j))
            Next
            ts.WriteLine line
        Next
        n = (n1 + 1) * (n2 + 1)
    Else
        ts.WriteLine "# data_dims=" & (n1 + 1)
        line = ""
        For i = 0 To n1
            If line <> "" Then line = line & ","
            line = line & Fmt(data(i))
            If (i + 1) Mod 256 = 0 Then
                ts.WriteLine line
                line = ""
                BeatNow
            End If
        Next
        If line <> "" Then ts.WriteLine line
        n = n1 + 1
    End If
    ts.Close
    If fso.FileExists(outPath) Then fso.DeleteFile outPath, True
    fso.MoveFile tmp, outPath
    If Err.Number <> 0 Then
        Fail res, ErrText("cannot write " & outPath)
        Err.Clear
        Exit Function
    End If
    On Error GoTo 0
    res("pixels") = n
    WriteData = True
End Function

' Waits `seconds`, answering stop requests and writing the heartbeat; False when stopped (gStoppedBy says who)
Function WaitStoppable(seconds)
    Dim t0, who
    WaitStoppable = True
    t0 = Timer
    Do While Elapsed(t0) < seconds
        who = StopRequested()
        If who <> "" Then
            gStoppedBy = who
            WaitStoppable = False
            Exit Function
        End If
        BeatNow
        LabSpec.Pause 50
    Loop
End Function

' A copy of the current video frame (the live view goes on), or 0
Function GrabFrame(res)
    Dim vid, clone, p
    GrabFrame = 0
    On Error Resume Next
    vid = LabSpec.Video(LS_GET_VIDEO_ID)
    If Err.Number <> 0 Or vid <= 0 Then
        Err.Clear
        vid = LabSpec.GetActiveData("Video")
    End If
    clone = 0
    If vid > 0 Then clone = LabSpec.Exec(vid, LS_CLONE_DATA, p)
    If Err.Number <> 0 Or clone <= 0 Then
        Fail res, ErrText("no video frame (video id " & vid & ")")
        Err.Clear
        clone = 0
    End If
    On Error GoTo 0
    GrabFrame = clone
End Function

' A frame of the video camera. The live frame is cloned (the live view goes on), written for Python (out &
' ".video.txt") and saved by LabSpec in each image format it accepts (out & "." & format). With laser_s > 0 a frame
' is first taken without the laser (out & ".nolaser.video.txt"), then the laser shutter (motor m_laser_shutter) is
' opened in manual shutter mode for laser_s, the frame is taken while it is open, and the shutter is closed and the
' shutter mode restored on every path out (frames are written to disk only after it is closed).
Sub VVideo(cmd, res)
    Dim outBase, cam, vid, wasOn, clone, clone0, p, shutter, laserS, oldMode, newMode, fmts, i, r, settle
    Dim opened, ok, acqS, aid, tA, keepOn, t0v
    outBase = GetP(cmd, "out", "")
    acqS = ParseNum(GetP(cmd, "during_acq_s", "0"))
    cam = GetP(cmd, "camera", "")
    settle = ParseNum(GetP(cmd, "settle_s", "1.5"))
    laserS = ParseNum(GetP(cmd, "laser_s", "0"))
    shutter = GetP(cmd, "m_laser_shutter", "")
    fmts = Split(GetP(cmd, "formats", "png,jpg,bmp,tif"), ",")
    If outBase = "" Then
        Fail res, "video needs 'out'"
        Exit Sub
    End If
    If laserS > 0 And shutter = "" Then
        Fail res, "video with the laser needs the laser shutter's motor name (profile motors.laser_shutter)"
        Exit Sub
    End If
    opened = False
    ok = True
    On Error Resume Next
    wasOn = False
    vid = VidId()
    If vid > 0 Then wasOn = True
    Err.Clear
    r = CameraFor(cam, wasOn, GetP(cmd, "switch_camera", "0") = "1", GetP(cmd, "stop_video", "0") = "1", res)
    If r <> "" Then                         ' no frame from another camera than the one asked
        Fail res, r
        Exit Sub
    End If
    keepOn = (CStr(res("camera_restart")) = "1")      ' it ran before the switch: it stays on afterwards
    If keepOn Then wasOn = False
    clone = 0
    If GetP(cmd, "extended", "0") = "1" Then
        ' LabSpec's extended video: a still image stitched over the area and settings made in LabSpec's interface
        ' (SetExtendedVideo, which would set them, is in the manual but not in this build). The stage moves.
        If laserS > 0 Or acqS > 0 Then
            Fail res, "extended video takes no laser (laser_s, during_acq_s)"
            Exit Sub
        End If
        If gVidOff Then
            Fail res, "the bridge stopped the live video: start it first (instrument camera_on), then the extended video"
            Exit Sub
        End If
        tA = Timer
        r = LabSpec.Video(LS_START_EXTENDED_VIDEO)
        If Err.Number <> 0 Or r = -1 Then
            Fail res, ErrText("extended video (returned " & r & ")")
            Err.Clear
            Exit Sub
        End If
        res("extended") = 1
        res("extended_returned") = r
        res("extended_s") = Fmt(Elapsed(tA))
        ' a returned id > 0 is the image; otherwise the active video image
        If r > 0 Then clone = LabSpec.Exec(r, LS_CLONE_DATA, p)
        If Err.Number <> 0 Then
            clone = 0
            Err.Clear
        End If
        wasOn = True
    ElseIf Not wasOn Then
        VidSettle
        LabSpec.Video LS_START_VIDEO
        If Err.Number <> 0 Then
            Fail res, ErrText("Video start")
            Err.Clear
            Exit Sub
        End If
        gVidOff = False
        t0v = Timer
        Do                                  ' as HORIBA: GET_VIDEO_ID polled after the start until > 0
            LabSpec.Pause 100
            vid = LabSpec.Video(LS_GET_VIDEO_ID)
            If Err.Number <> 0 Then
                vid = 0
                Err.Clear
            End If
            If vid > 0 Or Elapsed(t0v) > 5 Then Exit Do
        Loop
        r = CameraCheck(res)
        If r <> "" Then
            Fail res, r
            Exit Sub
        End If
    End If
    ' let the camera settle (exposure, live image)
    ok = WaitStoppable(settle)
    clone0 = 0
    If ok And laserS > 0 Then
        ' a frame without the laser first: the difference with the lit frame shows the spot alone
        clone0 = GrabFrame(res)
        oldMode = LSValue(0, "ShutterMode")
        newMode = LS_SHUTTER_MANUAL
        LabSpec.PutValue 0, "ShutterMode", newMode
        LabSpec.MoveMotor shutter, 1, "", LS_MOTOR_VALUE
        opened = True                     ' close it below, even if opening reported an error
        If Err.Number <> 0 Then
            Fail res, ErrText("opening the laser shutter (" & shutter & ")")
            Err.Clear
            ok = False
        End If
        If ok Then ok = WaitStoppable(laserS)
    End If
    If ok And acqS > 0 Then
        ' the beam during a Raman acquisition (LabSpec opens the shutter itself): a frame before, one half-way
        clone0 = GrabFrame(res)
        aid = LabSpec.GetAcqID()
        LabSpec.Acq LS_ACQ_SPECTRUM + LS_ACQ_NO_SPIKE_REMOVING, acqS, 1, ParseNum(GetP(cmd, "acq_from_nm", "0")), _
                    ParseNum(GetP(cmd, "acq_to_nm", "0"))
        If Err.Number <> 0 Then
            Fail res, ErrText("Acq (for the frame during an acquisition)")
            Err.Clear
            ok = False
        End If
        If ok Then ok = WaitStoppable(acqS / 2)
        If ok Then clone = GrabFrame(res)
        tA = Timer
        Do
            aid = LabSpec.GetAcqID()
            If Err.Number <> 0 Or aid > 0 Or aid = -2 Then Exit Do
            If Not ok Or Elapsed(tA) > acqS + 30 Then
                LabSpec.Acq LS_ACQ_CANCEL, 0, 0, 0, 0
                Exit Do
            End If
            BeatNow
            LabSpec.Pause 50
        Loop
        If aid > 0 Then LabSpec.Exec aid, LS_REMOVE_DATA, p
        Err.Clear
        res("during_acq_s") = acqS
    End If
    If ok And clone = 0 Then clone = GrabFrame(res)
    ' the laser off first, whatever happened
    If opened Then
        LabSpec.MoveMotor shutter, 0, "", LS_MOTOR_VALUE
        If CStr(oldMode) <> "" Then
            newMode = oldMode
            LabSpec.PutValue 0, "ShutterMode", newMode
        End If
        res("shutter_closed") = Bit(Err.Number = 0)
        Err.Clear
    End If
    res("laser_s") = laserS
    If Not wasOn And Not keepOn Then
        If GetP(cmd, "stop_video", "0") = "1" Then
            res("video_stopped") = Bit(StopVideoHoriba(res))
        Else
            res("video_left_on") = 1         ' not stopped from a script unless allowed: Video(STOP_VIDEO) hung
        End If
    End If
    Err.Clear
    On Error GoTo 0
    If clone0 > 0 Then
        If WriteImage(clone0, outBase & ".nolaser.video.txt", res) Then res("out_nolaser") = outBase & ".nolaser.video.txt"
        On Error Resume Next
        LabSpec.Exec clone0, LS_REMOVE_DATA, p
        Err.Clear
        On Error GoTo 0
    End If
    If clone > 0 Then
        res("id") = clone
        If WriteImage(clone, outBase & ".video.txt", res) Then res("out") = outBase & ".video.txt"
        On Error Resume Next
        For i = 0 To UBound(fmts)
            r = LabSpec.Save(clone, outBase & "." & fmts(i), fmts(i))
            res("saved_" & fmts(i)) = Bit(Err.Number = 0 And r = 0)
            Err.Clear
        Next
        LabSpec.Exec clone, LS_REMOVE_DATA, p
        Err.Clear
        On Error GoTo 0
    End If
    res("x_um") = MotorValue(GetP(cmd, "m_x", "X"))
    res("y_um") = MotorValue(GetP(cmd, "m_y", "Y"))
    res("objective") = MotorLabel(GetP(cmd, "m_objective", "Objective"))
End Sub

' ---------------------------------------------------------------- guided adjustment
' The signal of a line in spectrum `id`: (highest point in the window - the window's edge level) / exposure, after a
' 3-point median so a cosmic ray (one or two pixels; the loop runs without spike removal) does not count.
' The window is given in nm and in 1/cm; the one matching the spectrum's x unit is used. ymax: the spectrum's top.
Function GuideMetric(id, loNm, hiNm, loCm, hiCm, ex, ByRef ymax)
    Dim xy, at, au, i, n, xu, lo, hi, peak, edge, ne, xs(), ys(), md(), m, k, w, e, a, b, c
    GuideMetric = -1
    ymax = 0
    On Error Resume Next
    LabSpec.GetValue id, "XYData", xy
    If Err.Number <> 0 Or Not IsArray(xy) Then
        Err.Clear
        Exit Function
    End If
    xu = "nm"
    LabSpec.GetValue id, "AxisType", at
    LabSpec.GetValue id, "AxisUnits", au
    If IsArray(at) And IsArray(au) Then
        For i = 0 To UBound(at)
            If CStr(at(i)) = "Spectr" Then xu = LCase(CStr(au(i)))
        Next
    End If
    Err.Clear
    On Error GoTo 0
    lo = loNm
    hi = hiNm
    If InStr(xu, "cm") > 0 Then
        lo = loCm
        hi = hiCm
    End If
    n = UBound(xy, 2)
    ReDim xs(n)
    ReDim ys(n)
    m = -1
    For i = 0 To n
        If xy(1, i) > ymax Then ymax = xy(1, i)
        If xy(0, i) >= lo And xy(0, i) <= hi Then
            m = m + 1
            xs(m) = xy(0, i)
            ys(m) = xy(1, i)
        End If
    Next
    If m < 4 Then Exit Function
    ReDim md(m)
    For i = 0 To m
        a = ys(i)
        b = ys(i)
        c = ys(i)
        If i > 0 Then a = ys(i - 1)
        If i < m Then c = ys(i + 1)
        ' the median of three
        If (a - b) * (c - a) >= 0 Then
            md(i) = a
        ElseIf (b - a) * (c - b) >= 0 Then
            md(i) = b
        Else
            md(i) = c
        End If
    Next
    peak = md(0)
    For i = 0 To m
        If md(i) > peak Then peak = md(i)
    Next
    ' the edge level: the mean of the outer sixth of the window on each side
    w = Int((m + 1) / 6)
    If w < 1 Then w = 1
    edge = 0
    ne = 0
    For k = 0 To w - 1
        edge = edge + md(k) + md(m - k)
        ne = ne + 2
    Next
    edge = edge / ne
    e = ex
    If e <= 0 Then e = 1
    GuideMetric = (peak - edge) / e
End Function

Function NumText(v)
    If Abs(v) >= 100 Then
        NumText = Fmt(Round(v, 0))
    Else
        NumText = Fmt(Round(v, 2))
    End If
End Function

' A fast closed loop for a manual adjustment: short acquisitions, the line's signal after each, shown live to the
' operator (a LabSpec dialog, or the status bar), until the goal is met; then STOP is shown. Modes: reach (signal
' >= target), drop (signal <= target), maximize (the signal rose and has just fallen drop_frac below its best:
' the best position was passed). A spectrum near saturation stops at once. The operator ends it with Done or
' Abort; ls6t with stop.flag; max_s limits it.
Sub VGuide(cmd, res)
    Dim txt, mode, target, f, t, loNm, hiNm, loCm, hiCm, ex, maxS, stableN, dropF, sat, outPath, ui, unitL
    Dim useDlg, st, t0, n, nOk, nDrop, maxv, tmax, reason, id, prev, v, ymax, ts, tmp, tt, who, tA, show
    Dim stopMsg, tgtText, dlgClosed, p, t1, rose
    txt = GetP(cmd, "text", "Adjust slowly")
    mode = GetP(cmd, "mode", "reach")
    target = ParseNum(GetP(cmd, "target", "0"))
    f = ParseNum(GetP(cmd, "from_nm", "0"))
    t = ParseNum(GetP(cmd, "to_nm", "0"))
    loNm = ParseNum(GetP(cmd, "lo_nm", "0"))
    hiNm = ParseNum(GetP(cmd, "hi_nm", "0"))
    loCm = ParseNum(GetP(cmd, "lo_cm1", "0"))
    hiCm = ParseNum(GetP(cmd, "hi_cm1", "0"))
    ex = ParseNum(GetP(cmd, "exposure_s", "0.2"))
    maxS = ParseNum(GetP(cmd, "max_s", "120"))
    stableN = CLng(ParseNum(GetP(cmd, "stable_n", "2")))
    dropF = ParseNum(GetP(cmd, "drop_frac", "0.1"))
    sat = ParseNum(GetP(cmd, "sat", "60000"))
    outPath = GetP(cmd, "out", "")
    ui = GetP(cmd, "ui", "dialog")
    unitL = GetP(cmd, "unit", "counts/s")
    If outPath = "" Then
        Fail res, "guide needs 'out'"
        Exit Sub
    End If
    If mode = "maximize" Then
        tgtText = "the best (ls6t says STOP just after it)"
    ElseIf mode = "drop" Then
        tgtText = "<= " & NumText(target) & " " & unitL
    Else
        tgtText = ">= " & NumText(target) & " " & unitL
    End If
    ' the operator's panel
    useDlg = (ui = "dialog")
    dlgClosed = False
    If useDlg Then
        On Error Resume Next
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Dialog", "ls6t guide", "", "Width:60"
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Label", "instr", "ls6t: " & txt, ""
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "BeginTable", "", "", "Cols:2"
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Label", "l1", "Signal now:", ""
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Edit", "value", "-", "Disable:1;Width:40"
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Label", "l2", "Goal:", ""
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Edit", "goal", tgtText, "Disable:1;Width:40"
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Label", "l3", "Status:", ""
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Edit", "state", "measuring - keep adjusting slowly", "Disable:1;Width:40"
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "EndTable", "", "", ""
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Button", "Done", "Done", "CloseDialog:Yes;ReturnValue:1"
        LabSpec.Dialog LS_DIALOG_ADD_ITEM, "Button", "Abort", "Abort", "CloseDialog:Yes;ReturnValue:2"
        st = LabSpec.Dialog(LS_DIALOG_SHOW, "", "", "", "Dynamic:Yes")
        If Err.Number <> 0 Then
            useDlg = False
            res("ui_fallback") = ErrText("Dialog")
            Err.Clear
        End If
        On Error GoTo 0
    End If
    If Not useDlg Then LabSpec.Message "ls6t: " & txt & " | goal " & tgtText, LS_MB_STATUS_BAR
    tmp = outPath & ".tmp"
    Set ts = fso.CreateTextFile(tmp, True, True)
    ts.WriteLine "# t_s,value,ymax"
    t0 = Timer
    n = 0
    nOk = 0
    nDrop = 0
    maxv = -1E+300
    tmax = 0
    rose = False
    reason = ""
    prev = 0
    On Error Resume Next
    Do
        who = StopRequested()
        If who <> "" Then
            gStoppedBy = who
            reason = "stopped"
            Exit Do
        End If
        If useDlg Then
            st = LabSpec.Dialog(LS_DIALOG_GET_STATUS, "", "", "", "")
            If Err.Number = 0 And st >= 0 Then
                dlgClosed = True
                If st = 1 Then
                    reason = "operator_done"
                Else
                    reason = "operator_abort"
                End If
                Exit Do
            End If
            Err.Clear
        End If
        If Elapsed(t0) > maxS Then
            reason = "timeout"
            Exit Do
        End If
        ' one short acquisition (no spike removal: speed)
        id = LabSpec.GetAcqID()
        LabSpec.Acq LS_ACQ_SPECTRUM + LS_ACQ_AUTO_SHOW + LS_ACQ_NO_SPIKE_REMOVING, ex, 1, f, t
        If Err.Number <> 0 Then
            Fail res, ErrText("Acq")
            Err.Clear
            reason = "error"
            Exit Do
        End If
        tA = Timer
        Do
            id = LabSpec.GetAcqID()
            If id > 0 Then Exit Do
            who = StopRequested()
            If who <> "" Or Elapsed(tA) > ex + 30 Then
                LabSpec.Acq LS_ACQ_CANCEL, 0, 0, 0, 0
                If who <> "" Then gStoppedBy = who
                id = -9
                Exit Do
            End If
            BeatNow
            LabSpec.Pause 20
        Loop
        If id <= 0 Then
            If gStoppedBy <> "" Then
                reason = "stopped"
            Else
                reason = "error"
                Fail res, "an acquisition gave no data"
            End If
            Exit Do
        End If
        v = GuideMetric(id, loNm, hiNm, loCm, hiCm, ex, ymax)
        If prev > 0 Then LabSpec.Exec prev, LS_REMOVE_DATA, p
        prev = id
        Err.Clear
        n = n + 1
        tt = Elapsed(t0)
        ts.WriteLine Fmt(Round(tt, 3)) & "," & Fmt(v) & "," & Fmt(ymax)
        If v > maxv Then
            maxv = v
            tmax = tt
        End If
        ' the decision
        If ymax >= sat Then
            reason = "saturation"
        ElseIf mode = "maximize" Then
            If n >= 3 And v > 0.5 * maxv Then rose = True
            If rose And maxv > 0 And v < (1 - dropF) * maxv Then
                nDrop = nDrop + 1
            Else
                nDrop = 0
            End If
            If nDrop >= 2 Then reason = "past_max"
        Else
            If (mode = "drop" And v <= target) Or (mode <> "drop" And v >= target) Then
                nOk = nOk + 1
            Else
                nOk = 0
            End If
            If nOk >= stableN Then reason = "target"
        End If
        show = NumText(v) & " " & unitL & "   (best " & NumText(maxv) & ")"
        If useDlg Then
            LabSpec.Dialog LS_DIALOG_UPDATE_VAR, "", "value", show, ""
        Else
            LabSpec.Message "ls6t: " & show & " | goal " & tgtText, LS_MB_STATUS_BAR
        End If
        Err.Clear
        If reason <> "" Then Exit Do
        BeatNow
    Loop
    ts.Close
    If fso.FileExists(outPath) Then fso.DeleteFile outPath, True
    fso.MoveFile tmp, outPath
    Err.Clear
    ' STOP
    stopMsg = ""
    If reason = "target" Then stopMsg = "STOP - goal reached (" & NumText(v) & " " & unitL & ")"
    If reason = "past_max" Then stopMsg = "STOP - you just passed the best (" & NumText(maxv) & "): go back slightly"
    If reason = "saturation" Then stopMsg = "STOP - the detector is near saturation: reduce the power"
    If reason = "timeout" Then stopMsg = "Time is up - stop adjusting"
    If stopMsg <> "" Then
        LabSpec.Message stopMsg, LS_MB_STATUS_BAR
        If useDlg And Not dlgClosed Then
            LabSpec.Dialog LS_DIALOG_UPDATE_VAR, "", "state", stopMsg, ""
            ' wait (at most 2 minutes) for the operator to close the panel
            t1 = Timer
            Do
                st = LabSpec.Dialog(LS_DIALOG_GET_STATUS, "", "", "", "")
                If Err.Number <> 0 Or st >= 0 Then Exit Do
                If Elapsed(t1) > 120 Then Exit Do
                BeatNow
                LabSpec.Pause 100
            Loop
            Err.Clear
        Else
            LabSpec.Message stopMsg, LS_MB_OK + LS_MB_ICONWARNING
        End If
    End If
    Err.Clear
    On Error GoTo 0
    res("reason") = reason
    res("value") = v
    If n > 0 Then
        res("max_value") = maxv
        res("t_max_s") = tmax
    End If
    res("iterations") = n
    If n > 0 Then res("period_s") = Elapsed(t0) / n
    res("out") = outPath
    res("message") = stopMsg
End Sub

' A message the operator acknowledges (OK or Cancel), e.g. a checklist before a step only they can do
Sub VNotice(cmd, res)
    Dim b
    On Error Resume Next
    b = LabSpec.Message(GetP(cmd, "text", ""), LS_MB_OKCANCEL + LS_MB_ICONINFORMATION)
    If Err.Number <> 0 Then
        Fail res, ErrText("Message")
        Err.Clear
        Exit Sub
    End If
    On Error GoTo 0
    If b = 0 Then
        res("answer") = "ok"
    Else
        res("answer") = "cancel"
    End If
End Sub

' A map run by LabSpec's own engine. Axes: x/y/z (from, to, step in LabSpec's stage units, as ls6t computed them),
' each on its motor (m_x, m_y, m_z); the spectrometer centred at center_nm (one window); exposure and accumulations
' set in LabSpec's interface; SWIFT on or off; then ACQ_SPECTRAL_IMAGE and a wait (stop checks, heartbeat). The
' map is written for ls6t (out & ".map.txt": axes and values) and saved as .l6s (and LabSpec's text export).
Sub VMapNative(cmd, res)
    Dim ax, i, mname, f, t, st, n, ex, acc, sw, swOk, id, t0, started, who, tmo, outBase, r, mode, tval, axes
    outBase = GetP(cmd, "out", "")
    gChunk = CLng(ParseNum(GetP(cmd, "chunk", "65536")))
    ex = ParseNum(GetP(cmd, "exposure_s", "1"))
    acc = CLng(ParseNum(GetP(cmd, "accumulations", "1")))
    tmo = ParseNum(GetP(cmd, "timeout_s", "36000"))
    If outBase = "" Then
        Fail res, "map_native needs 'out'"
        Exit Sub
    End If
    On Error Resume Next
    ' the spectrometer centre (one window)
    If GetP(cmd, "center_nm", "") <> "" Then
        r = LabSpec.MoveMotor("Spectro", ParseNum(GetP(cmd, "center_nm", "0")), "", LS_MOTOR_VALUE + LS_MOTOR_NO_WAIT)
        If Err.Number <> 0 Then
            Fail res, ErrText("Spectro")
            Err.Clear
            Exit Sub
        End If
        If r > 0 Then WaitMotor "Spectro", r, 120, res
    End If
    ' the axes: all off, then the ones asked for
    LabSpec.SetMappingParams "", 0, 0, 0, LS_DISABLE_ALL_MOTORS, 0
    axes = ""
    For Each ax In Array("x", "y", "z")
        If GetP(cmd, ax & "_from", "") <> "" Then
            mname = GetP(cmd, "m_" & ax, UCase(ax))
            f = ParseNum(GetP(cmd, ax & "_from", "0"))
            t = ParseNum(GetP(cmd, ax & "_to", "0"))
            st = ParseNum(GetP(cmd, ax & "_step", "1"))
            LabSpec.SetMappingParams mname, f, t, st, LS_INCREMENT_STEP, LS_ENABLE_AXIS
            axes = axes & mname & " "
        End If
    Next
    If Err.Number <> 0 Then
        Fail res, ErrText("SetMappingParams")
        Err.Clear
        Exit Sub
    End If
    res("axes") = Trim(axes)
    ' exposure and accumulations in the interface; SWIFT
    LabSpec.PutValue 0, "LabSpecExposureTime", ex
    LabSpec.PutValue 0, "LabSpecAccumulation", acc
    sw = GetP(cmd, "swift", "0")
    LabSpec.GetValue 0, "SWIFT", swOk
    Err.Clear
    If sw = "1" Then
        If CStr(swOk) = "-1" Then
            Fail res, "SWIFT is not available with this configuration"
            Exit Sub
        End If
        n = 1
        LabSpec.PutValue 0, "SWIFT", n
    Else
        n = 0
        LabSpec.PutValue 0, "SWIFT", n
    End If
    Err.Clear
    ' start
    mode = LS_ACQ_SPECTRAL_IMAGE + CLng(ParseNum(GetP(cmd, "mode_flags", "0")))
    tval = -1
    If GetP(cmd, "acq_time", "interface") = "exposure" Then tval = ex
    For i = 1 To 50
        id = LabSpec.GetAcqID()
        If Err.Number <> 0 Or id = -1 Then Exit For
        LabSpec.Pause 20
    Next
    Err.Clear
    LabSpec.Acq mode, tval, acc, ParseNum(GetP(cmd, "from_nm", "0")), ParseNum(GetP(cmd, "to_nm", "0"))
    If Err.Number <> 0 Then
        Fail res, ErrText("Acq (spectral image)")
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
        If id = -2 And (started Or Elapsed(t0) > 1) Then
            gHalted = True
            gStoppedBy = "operator"
            ClosePopup
            Exit Sub
        End If
        If id = 0 Then started = True
        If id = -1 And Not started And Elapsed(t0) > 30 Then
            Fail res, "the map did not start (LabSpec reports no acquisition in progress)"
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
            Fail res, "the map did not finish in " & tmo & " s (cancelled)"
            Exit Sub
        End If
        BeatNow
        LabSpec.Pause 200
    Loop
    Err.Clear
    On Error GoTo 0
    res("id") = id
    res("seconds_map") = Elapsed(t0)
    If WriteData(id, outBase & ".map.txt", res, "map") Then res("out") = outBase & ".map.txt"
    On Error Resume Next
    r = LabSpec.Save(id, outBase & ".l6s", "")
    res("native_ok") = Bit(Err.Number = 0 And r = 0)
    Err.Clear
    r = LabSpec.Save(id, outBase & ".labspec.txt", "txt")
    res("text_ok") = Bit(Err.Number = 0 And r = 0)
    Err.Clear
    LabSpec.Exec id, LS_SHOW_DATA, r
    Err.Clear
    On Error GoTo 0
    res("detector_temp_c") = DetectorTemp()
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

' The laser left on for viewing (verb laser): the shutter in manual mode, open for at most `seconds`. The bridge
' closes it when the time is up, when the operator halts or stops the bridge, and before any other command but
' a few quick reads (LaserGuard), so nothing else ever runs with it open.
Sub LaserClose(why)
    Dim m
    If Not gLaserOpen Then Exit Sub
    On Error Resume Next
    LabSpec.MoveMotor gLaserMotor, 0, "", LS_MOTOR_VALUE
    If CStr(gLaserOldMode) <> "" Then
        m = gLaserOldMode
        LabSpec.PutValue 0, "ShutterMode", m
    End If
    Err.Clear
    On Error GoTo 0
    gLaserOpen = False
    LabSpec.Message "ls6t: laser shutter closed (" & why & ")", LS_MB_STATUS_BAR
End Sub

Sub LaserGuard(verb)
    If gLaserOpen And InStr(",ping,status,camera,labels,last_error,message,laser,", "," & verb & ",") = 0 Then
        LaserClose "before " & verb
    End If
End Sub

Sub VLaser(cmd, res)
    Dim state, secs, shutter, newMode
    state = GetP(cmd, "state", "")
    If state = "off" Then
        res("was_open") = Bit(gLaserOpen)
        LaserClose "asked"
        res("open") = 0
        Exit Sub
    End If
    If state <> "on" Then
        Fail res, "laser state: on or off"
        Exit Sub
    End If
    shutter = GetP(cmd, "m_laser_shutter", "")
    secs = ParseNum(GetP(cmd, "seconds", "0"))
    If shutter = "" Or secs <= 0 Then
        Fail res, "laser on needs m_laser_shutter and seconds"
        Exit Sub
    End If
    On Error Resume Next
    If Not gLaserOpen Then
        gLaserOldMode = LSValue(0, "ShutterMode")
        newMode = LS_SHUTTER_MANUAL
        LabSpec.PutValue 0, "ShutterMode", newMode
        gLaserMotor = shutter
        gLaserOpen = True                     ' closed by LaserClose on every path, even if opening failed
        LabSpec.MoveMotor shutter, 1, "", LS_MOTOR_VALUE
        If Err.Number <> 0 Then
            Fail res, ErrText("opening the laser shutter (" & shutter & ")")
            Err.Clear
            On Error GoTo 0
            LaserClose "error"
            Exit Sub
        End If
    End If
    Err.Clear
    On Error GoTo 0
    gLaserT0 = Timer
    gLaserS = secs
    res("open") = 1
    res("seconds") = Fmt(secs)
    LabSpec.Message "ls6t: laser shutter OPEN for " & Fmt(secs) & " s (close the ls6t window to stop)", LS_MB_STATUS_BAR
End Sub

Sub VSafe(cmd, res)
    Dim st
    LaserClose "safe"
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

' The operator's Yes to go on. Timed: prompt_shown_after_s (from the command's arrival to the question being put to
' LabSpec) and answer_after_s (the question on screen until answered). On .42/.43 a resume took 58.8 and 99.6 s with
' the bridge idle and halted; the HALTED window was still open over it: it is now closed while the question is
' asked (and opened again on No), so the two do not overlap.
Sub VResume(cmd, res)
    Dim b, t0, t1
    t0 = gCmdT0
    If Not gHalted Then
        res("halted") = 0
        Exit Sub
    End If
    ClosePopup                              ' the HALTED window: not over the question
    On Error Resume Next
    t1 = Timer
    res("prompt_shown_after_s") = Fmt(Elapsed(t0))
    b = LabSpec.Message("ls6t asks to control the instrument again. Allow it?", LS_MB_YESNO + LS_MB_ICONQUESTION)
    res("answer_after_s") = Fmt(Elapsed(t1))
    Err.Clear
    On Error GoTo 0
    If b = LS_IDYES Then
        gHalted = False
        OpenMainPopup True                  ' says it resumed: not a new request
        res("answer") = "yes"
    Else
        OpenHaltedPopup
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
    gCmdT0 = Timer
    Set res = CreateObject("Scripting.Dictionary")
    res("seq") = gSeq
    res("ok") = 1
    res("error") = ""
    gStoppedBy = ""
    Beat "busy", True
    LaserGuard verb
    If gHalted And InStr(",ping,status,resume,quit,safe,message,", "," & verb & ",") = 0 Then
        Fail res, "halted by the operator: resume in LabSpec"
    Else
        Select Case verb
            Case "laser": VLaser cmd, res
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
            Case "video": VVideo cmd, res
            Case "camera": VCamera cmd, res
            Case "labels": VLabels cmd, res
            Case "calib_params": VCalibParams cmd, res
            Case "run_vbs": VRunVbs cmd, res
            Case "map_native": VMapNative cmd, res
            Case "config": VConfig cmd, res
            Case "last_error": res("info") = LabSpecError()
            Case "guide": VGuide cmd, res
            Case "notice": VNotice cmd, res
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

' The first motor command after a start was slow three times (2026-11-20: 36.6 s filter, 24.1 s and 31.7 s grating),
' the time spent in GetMotorStatus, not in MoveMotor. At start the bridge asks each motor's status once (nothing
' moves), waiting while LabSpec says busy (at most 120 s each: the grating took 36.0 and 50.9 s), so any first-call delay is spent here rather than
' in the first request. The times go to ping (warmup) and LS6T_HOME\warmup.txt. TESTING T3.16.
Dim gWarm
gWarm = ""
Sub WarmUp()
    Dim names, i, t0, st, nm, o
    gVerb = "warmup"
    Beat "busy", True
    names = Array("Grating", "Filter", "Laser", "Hole", "Slit", "Spectro")
    gWarm = "started=" & Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & "T" & _
            Right("0" & Hour(Now), 2) & ":" & Right("0" & Minute(Now), 2) & ":" & Right("0" & Second(Now), 2) & ";"
    On Error Resume Next
    gWarm = gWarm & "grating_at=" & MotorValue("Grating") & ";"     ' where the grating was left
    For i = 0 To UBound(names)
        nm = names(i)
        If MotorSize(nm) <> -1 Then
            t0 = Timer
            Do
                st = LabSpec.GetMotorStatus(nm, gNoId)
                If Err.Number <> 0 Then
                    Err.Clear
                    Exit Do
                End If
                If st = 0 Or Elapsed(t0) > 120 Then Exit Do
                BeatNow
                o = OperatorStop()          ' Halt or the stop button: the warm-up ends there
                If o <> "" Then
                    gHalted = True
                    If o = "quit" Then gQuit = True
                    If o = "operator" Then OpenHaltedPopup
                    Exit Do
                End If
                LabSpec.Pause 100
            Loop
            gWarm = gWarm & nm & "=" & Fmt(Elapsed(t0)) & ";"
            If gHalted Or gQuit Then Exit For
        End If
    Next
    Err.Clear
    DiscAppend LS6T_HOME_DIR & "\warmup.txt", Now & " " & LS6T_VERSION & " " & gWarm
    On Error GoTo 0
End Sub

' ---------------------------------------------------------------- main loop
Function IIfHalted(st)
    If st = "halted" Then
        IIfHalted = "It is HALTED: to go on, ls6t instrument resume; to end it, close its 'HALTED' window (Cancel) " & _
                    "or run ls6t instrument end-bridge, then start ls6t_bridge.vbs again." & vbCrLf & vbCrLf
    Else
        IIfHalted = "To restart it: ls6t instrument end-bridge, then start ls6t_bridge.vbs again." & vbCrLf & vbCrLf
    End If
End Function

' Another bridge already running in this LabSpec (its heartbeat fresh, not stopped): this one says so and ends
Function OtherBridge()
    Dim f, st, age, d
    OtherBridge = ""
    On Error Resume Next
    If fso.FileExists(gHb) Then
        Set f = fso.GetFile(gHb)
        age = DateDiff("s", f.DateLastModified, Now)
        Set d = ReadKV(gHb)
        st = d("state")
        If Err.Number = 0 And age <= 5 And st <> "stopped" And st <> "" Then OtherBridge = st
    End If
    Err.Clear
    On Error GoTo 0
End Function

Sub BridgeMain()
    Dim p, o, other
    other = OtherBridge()
    If other <> "" Then
        LabSpec.Message "An ls6t bridge is already running in LabSpec (" & other & ")." & vbCrLf & vbCrLf & _
                        IIfHalted(other) & "This second start does nothing.", LS_MB_OK
        Exit Sub
    End If
    EnsureFolder LS6T_BRIDGE_DIR
    EnsureFolder gInbox
    EnsureFolder gOutbox
    If fso.FileExists(gStopFlag) Then fso.DeleteFile gStopFlag, True
    OpenPopup
    LabSpec.Message "ls6t bridge " & LS6T_VERSION & " running (" & LS6T_BRIDGE_DIR & ")", LS_MB_STATUS_BAR
    ' the instrument's inventory (motors, positions, detector; no probes, nothing moves): ls6t builds or checks its
    ' machine profile from it
    On Error Resume Next
    gVerb = "inventory"                 ' in the heartbeat: a bridge stuck here is told apart from one in a command
    Beat "busy", True
    DiscoverTo LS6T_HOME_DIR & "\discovery_latest.txt", False
    Err.Clear
    On Error GoTo 0
    WarmUp
    gVerb = ""
    Beat "idle", True
    Do While Not gQuit
        BeatNow
        If fso.FileExists(gStopFlag) Then StopRequested       ' between commands a stop.flag is stale: drop it
        If gHalted And Not gPopupOpen Then OpenHaltedPopup
        o = OperatorStop()      ' between commands: the window closed (halt; when halted: end) or LabSpec's stop button
        If o = "quit" Then gQuit = True
        If o = "operator" Then
            gHalted = True
            LabSpec.Message "ls6t halted by the operator: it waits for a resume", LS_MB_STATUS_BAR
            OpenHaltedPopup
        End If
        If gLaserOpen Then
            If gHalted Or gQuit Then
                LaserClose "operator"
            ElseIf Elapsed(gLaserT0) >= gLaserS Then
                LaserClose "time up"
            End If
        End If
        If gQuit Then Exit Do
        p = NextCommand()
        If p <> "" Then
            HandleFile p
        Else
            LabSpec.Pause POLL_MS
        End If
    Loop
    LaserClose "bridge stopped"
    ClosePopup
    gVerb = ""
    Beat "stopped", True
    LabSpec.Message "ls6t bridge stopped", LS_MB_STATUS_BAR
End Sub

BridgeMain
