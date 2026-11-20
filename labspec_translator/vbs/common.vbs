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
        ' INDEXTOSTRING (20+i) runs into FULL_INDEXTOVALUE (30) past index 9
        If idx >= 0 And idx <= 9 Then MotorLabel = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOSTRING + idx)
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

Function Bit(b)
    If b Then
        Bit = 1
    Else
        Bit = 0
    End If
End Function

' ---------------------------------------------------------------- discovery (no laser, no movement)
' withPopup: also test the non-blocking window (not from the bridge: it would close the bridge's own window)
Function DiscoverTo(outPath, withPopup, withProbes)
    Dim d, names, i, j, name, size, n, okPopup, ret, e0, probe, part, cpN, cpU, cpT, cpV, ramp, s
    Set d = CreateObject("Scripting.Dictionary")
    d("time") = Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & "T" & _
                Right("0" & Hour(Now), 2) & ":" & Right("0" & Minute(Now), 2) & ":" & Right("0" & Second(Now), 2)
    d("protocol") = LS6T_PROTOCOL
    d("bridge_version") = LS6T_VERSION
    d("system") = LSValue(0, "SystemName")
    d("labspec_path") = LSValue(0, "LabSpecPath")
    d("user_level") = LSValue(0, "UserLevel")
    d("exposure_s") = LSValue(0, "LabSpecExposureTime")
    d("accumulations") = LSValue(0, "LabSpecAccumulation")
    d("shutter_mode") = LSValue(0, "ShutterMode")
    d("detector_temp_c") = DetectorTemp()
    d("decimal_separator") = gSep
    names = Array("Laser", "Grating", "Filter", "Density", "ND", "Attenuator", "Objective", "Objectives", _
                  "Microscope", "Nosepiece", "Hole", "Slit", "Spectro", "Premono", "X", "Y", "Z", "XT", "YT", _
                  "XL", "YL", "Polarizer", "Raman Polarizer", "Laser Polarizer", "DetectorShutter", _
                  "Shutter", "LaserShutter", "Laser Shutter", "Laser shutter", "LaserPol", "RamanPol", _
                  "LaserFilter", "FilterV", "DuoPos", "Output", "Microscope", "IlluminatorV", "LaserSwitch", _
                  "Fiber", "E-Cal_Neon")
    For i = 0 To UBound(names)
        name = names(i)
        size = MotorSize(name)
        If size <> -1 Then
            d("motor." & name & ".present") = 1
            d("motor." & name & ".size") = size
            On Error Resume Next
            If size > 0 Then
                n = size
                If n > 10 Then n = 10      ' INDEXTOVALUE (10+i) and INDEXTOSTRING (20+i) overlap past 10
                For j = 0 To n - 1
                    d("motor." & name & ".value." & j) = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOVALUE + j)
                    d("motor." & name & ".label." & j) = LabSpec.GetMotorPosition(name, LS_MOTOR_INDEXTOSTRING + j)
                    If Err.Number <> 0 Then Err.Clear
                Next
                If size > 10 Then d("motor." & name & ".truncated") = 1
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
        End If
    Next
    ' self-tests for the bridge's design (no laser, no motion)
    On Error Resume Next
    LabSpec.Pause 10
    d("selftest.pause") = Bit(Err.Number = 0)
    Err.Clear
    If withPopup Then
        ret = LabSpec.Message("ls6t discovery: this window tests the bridge's stop window. It closes by itself.", _
                              LS_MB_NON_BLOCKING)
        LabSpec.Pause 300
        d("selftest.popup_open") = LabSpec.Message("", LS_MB_NON_BLOCKING)        ' -1 while open
        LabSpec.Message "#", LS_MB_NON_BLOCKING
        LabSpec.Pause 300
        d("selftest.popup_closed") = LabSpec.Message("", LS_MB_NON_BLOCKING)      ' 0 once closed
        d("selftest.popup_error") = Err.Description
        Err.Clear
    End If
    d("selftest.stop_button") = LabSpec.MessageEx("", LS_MB_STOP)
    d("selftest.stop_button_error") = Err.Description
    Err.Clear
    d("video.active_camera") = LabSpec.Video(LS_GET_ACTIVE_CAMERA)
    d("video.id_now") = LabSpec.Video(LS_GET_VIDEO_ID)          ' > 0 when the live video runs
    d("video.error") = Err.Description
    Err.Clear
    ret = LabSpec.GetLastErrorInfo()                            ' not in the 2018 manual; in the type library
    d("selftest.last_error_info") = Bit(Err.Number = 0)
    Err.Clear
    ' methods found in the type library (or in the manual but missing from some builds): read-only probes, not
    ' when the bridge takes its inventory at start (the scratch spectrum would flash a window)
    If withProbes Then
    ret = LabSpec.GetValueEx(0, "SystemName", e0, LS_VT_BSTR)
    d("selftest.get_value_ex") = Bit(Err.Number = 0)
    Err.Clear
    probe = LabSpec.CreateDataObject("Spectrum", 16, 0, -1)       ' a scratch spectrum, removed below
    If Err.Number = 0 And probe > 0 Then
        ' values 0..15, so the part read shows the units of StartAt and DataSize (values or bytes)
        ReDim ramp(15)
        For j = 0 To 15
            ramp(j) = CSng(j)
        Next
        LabSpec.PutValue probe, "Data", ramp
        d("selftest.put_value_ramp") = Bit(Err.Number = 0)
        Err.Clear
        part = LabSpec.GetValueSimplePart(probe, "Data", 0, LS_VT_R4, 4, 8)
        d("selftest.get_value_simple_part") = Bit(Err.Number = 0 And IsArray(part))
        If IsArray(part) Then
            d("selftest.get_value_simple_part_size") = UBound(part) - LBound(part) + 1
            s = ""
            For j = LBound(part) To UBound(part)
                If s <> "" Then s = s & ","
                s = s & Fmt(part(j))
            Next
            d("selftest.get_value_simple_part_values") = s
        End If
        Err.Clear
        LabSpec.Exec probe, LS_REMOVE_DATA, ret
    End If
    Err.Clear
    LabSpec.GetConfigParam cpN, cpU, cpT, cpV
    d("selftest.get_config_param") = Bit(Err.Number = 0)
    If Err.Number = 0 And IsArray(cpN) Then
        For j = LBound(cpN) To UBound(cpN)
            d("config_param." & j & ".name") = CStr(cpN(j))
            If IsArray(cpU) Then d("config_param." & j & ".unit") = CStr(cpU(j))
            If IsArray(cpT) Then d("config_param." & j & ".type") = CStr(cpT(j))
            If IsArray(cpV) Then d("config_param." & j & ".value") = CStr(cpV(j))
        Next
    End If
    Err.Clear
    End If
    LabSpec.Message "ls6t discovery running", LS_MB_STATUS_BAR
    d("selftest.status_bar") = Bit(Err.Number = 0)
    Err.Clear
    On Error GoTo 0
    WriteKV outPath, d
    DiscoverTo = outPath
End Function
