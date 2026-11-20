' ---------------------------------------------------------------------------------------------------------------
' script_motor_main.vbs - a virtual "script controlled motor" for LabSpec 6, to learn how LabSpec talks to one.
' Version: @@VERSION@@
'
' LabSpec 6 can have up to 5 motors driven by a script (Processing > Scripts, motor scripts). The script asks
' LabSpec what to do with GetMotorAction(Action, Position) and reports back with SetMotorInfo(Position, State).
' The meaning of the action codes and states is not documented, so this script drives no hardware: it logs every
' call (LS6T_HOME\script_motor_log.txt), keeps a virtual position (LS6T_HOME\script_motor_position.txt), and
' reports it back as reached. Attach it to a script motor, move that motor from LabSpec's interface (to a value,
' then to a named position, then stop it), and send the log to the ls6t maintainer: the codes it records let a
' later version drive real hardware (a rotating polariser, a heating stage...) the same way. TESTING.md, T1.4.
' ---------------------------------------------------------------------------------------------------------------

Sub ScriptMotorLog(text)
    Dim f, stamp
    On Error Resume Next
    EnsureFolder LS6T_HOME_DIR
    stamp = Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & " " & _
            Right("0" & Hour(Now), 2) & ":" & Right("0" & Minute(Now), 2) & ":" & Right("0" & Second(Now), 2)
    Set f = fso.OpenTextFile(LS6T_HOME_DIR & "\script_motor_log.txt", 8, True)
    f.WriteLine stamp & vbTab & text
    f.Close
    Err.Clear
    On Error GoTo 0
End Sub

Function DescribeVariant(v)
    Dim s, i
    If IsArray(v) Then
        s = "array(" & LBound(v) & ".." & UBound(v) & "):"
        For i = LBound(v) To UBound(v)
            s = s & " [" & i & "]=" & TypeName(v(i)) & ":" & CStr(v(i))
        Next
        DescribeVariant = s
    ElseIf IsEmpty(v) Then
        DescribeVariant = "Empty"
    Else
        DescribeVariant = TypeName(v) & ":" & CStr(v)
    End If
End Function

Sub ScriptMotorMain()
    Dim action, position, ret, posFile, f, current, state
    posFile = LS6T_HOME_DIR & "\script_motor_position.txt"
    current = 0
    On Error Resume Next
    If fso.FileExists(posFile) Then
        Set f = fso.OpenTextFile(posFile, 1)
        current = ParseNum(f.ReadLine)
        f.Close
    End If
    Err.Clear
    ret = LabSpec.GetMotorAction(action, position)
    If Err.Number <> 0 Then
        ScriptMotorLog "GetMotorAction failed: " & Err.Number & " " & Err.Description & LabSpecErrorInfoText()
        Err.Clear
        Exit Sub
    End If
    ScriptMotorLog "GetMotorAction returned " & DescribeVariant(ret) & "; Action=" & DescribeVariant(action) & _
                   "; Position=" & DescribeVariant(position) & "; virtual position was " & Fmt(current)
    ' a numeric target: take it as reached (the virtual motor moves at once)
    If Not IsArray(position) And Not IsEmpty(position) Then
        If IsNumeric(position) Then current = CDbl(position)
    End If
    Set f = fso.CreateTextFile(posFile, True)
    f.WriteLine Fmt(current)
    f.Close
    Err.Clear
    state = 0
    ret = LabSpec.SetMotorInfo(current, state)
    If Err.Number <> 0 Then
        ScriptMotorLog "SetMotorInfo(" & Fmt(current) & ", " & state & ") failed: " & Err.Number & " " & _
                       Err.Description & LabSpecErrorInfoText()
        Err.Clear
    Else
        ScriptMotorLog "SetMotorInfo(" & Fmt(current) & ", " & state & ") returned " & DescribeVariant(ret)
    End If
    On Error GoTo 0
End Sub

Function LabSpecErrorInfoText()
    Dim info
    LabSpecErrorInfoText = ""
    On Error Resume Next
    info = LabSpec.GetLastErrorInfo()
    If Err.Number = 0 Then LabSpecErrorInfoText = " [LabSpec: " & DescribeVariant(info) & "]"
    Err.Clear
    On Error GoTo 0
End Function

ScriptMotorMain
