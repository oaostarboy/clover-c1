// Read-only native session/token evidence. No logon, impersonation or token mutation.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Principal;

public static class TaskOriginSession {
    const int TokenUser=1, TokenGroups=2, TokenStatistics=10, TokenSessionId=12,
              TokenElevationType=18, TokenLinkedToken=19, TokenElevation=20, TokenIntegrityLevel=25;
    [StructLayout(LayoutKind.Sequential)] struct LUID { public uint Low; public int High; }
    [StructLayout(LayoutKind.Sequential)] struct SID_ATTRIBUTES { public IntPtr Sid; public uint Attributes; }
    [StructLayout(LayoutKind.Sequential)] struct GROUPS_ONE { public uint Count; public SID_ATTRIBUTES First; }
    [StructLayout(LayoutKind.Sequential)] struct STATS {
        public LUID TokenId, AuthenticationId; public long ExpirationTime;
        public int TokenType, ImpersonationLevel; public uint DynamicCharged, DynamicAvailable, GroupCount, PrivilegeCount;
        public LUID ModifiedId;
    }
    [StructLayout(LayoutKind.Sequential)] struct UNICODE_STRING { public ushort Length, MaximumLength; public IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)] struct LOGON_DATA {
        public uint Size; public LUID LogonId;
        public UNICODE_STRING UserName, LogonDomain, AuthenticationPackage;
        public uint LogonType, Session; public IntPtr Sid; public long LogonTime;
        public UNICODE_STRING LogonServer, DnsDomainName, Upn;
    }
    [StructLayout(LayoutKind.Sequential)] struct WTS_SESSION { public int Id; public IntPtr Station; public int State; }
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern uint WTSGetActiveConsoleSessionId();
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr data, int length, out int needed);
    [DllImport("secur32.dll")] static extern uint LsaGetLogonSessionData(ref LUID id, out IntPtr data);
    [DllImport("secur32.dll")] static extern uint LsaFreeReturnBuffer(IntPtr data);
    [DllImport("wtsapi32.dll", SetLastError=true)] static extern bool WTSEnumerateSessionsW(IntPtr server, int reserved, int version, out IntPtr data, out int count);
    [DllImport("wtsapi32.dll", SetLastError=true)] static extern bool WTSQuerySessionInformationW(IntPtr server, int session, int kind, out IntPtr data, out int bytes);
    [DllImport("wtsapi32.dll")] static extern void WTSFreeMemory(IntPtr data);

    static IntPtr Info(IntPtr token, int kind) {
        int size;
        GetTokenInformation(token, kind, IntPtr.Zero, 0, out size);
        int error=Marshal.GetLastWin32Error();
        if (size<=0 || error!=122) throw new Win32Exception(error);
        IntPtr data=Marshal.AllocHGlobal(size);
        if (!GetTokenInformation(token, kind, data, size, out size)) {
            error=Marshal.GetLastWin32Error(); Marshal.FreeHGlobal(data); throw new Win32Exception(error);
        }
        return data;
    }
    static int Number(IntPtr token, int kind) {
        IntPtr data=Info(token,kind); try { return Marshal.ReadInt32(data); } finally { Marshal.FreeHGlobal(data); }
    }
    static string Sid(IntPtr pointer) { return pointer==IntPtr.Zero ? null : new SecurityIdentifier(pointer).Value; }
    static string Error(Exception exc) {
        var win=exc as Win32Exception;
        return exc.GetType().Name + ":" + (win==null ? exc.HResult : win.NativeErrorCode).ToString();
    }
    static Dictionary<string,object> ReadToken(IntPtr token) {
        var row=new Dictionary<string,object>(); var errors=new List<string>(); row["errors"]=errors;
        try {
            IntPtr data=Info(token,TokenUser);
            try { row["sid"]=Sid(Marshal.ReadIntPtr(data)); } finally { Marshal.FreeHGlobal(data); }
            row["session_id"]=Number(token,TokenSessionId);
            row["elevation_type"]=Number(token,TokenElevationType);
            row["elevated"]=Number(token,TokenElevation)!=0;
            data=Info(token,TokenIntegrityLevel);
            try { row["integrity_sid"]=Sid(Marshal.ReadIntPtr(data)); } finally { Marshal.FreeHGlobal(data); }
            data=Info(token,TokenGroups);
            try {
                int count=Marshal.ReadInt32(data), offset=(int)Marshal.OffsetOf<GROUPS_ONE>("First");
                bool interactive=false;
                for(int i=0;i<count;i++) {
                    var group=Marshal.PtrToStructure<SID_ATTRIBUTES>(IntPtr.Add(data,offset+i*Marshal.SizeOf<SID_ATTRIBUTES>()));
                    // enabled group, not deny-only
                    if(Sid(group.Sid)=="S-1-5-4" && (group.Attributes&4)!=0 && (group.Attributes&16)==0) interactive=true;
                }
                row["interactive_group"]=interactive;
            } finally { Marshal.FreeHGlobal(data); }
            data=Info(token,TokenStatistics); STATS stats;
            try { stats=Marshal.PtrToStructure<STATS>(data); } finally { Marshal.FreeHGlobal(data); }
            row["authentication_luid"]=stats.AuthenticationId.High.ToString("x8")+stats.AuthenticationId.Low.ToString("x8");
            IntPtr logon;
            uint status=LsaGetLogonSessionData(ref stats.AuthenticationId,out logon);
            row["lsa_ntstatus"]=status;
            if(status!=0) throw new InvalidOperationException("LsaGetLogonSessionData:"+status.ToString("x8"));
            try {
                var info=Marshal.PtrToStructure<LOGON_DATA>(logon);
                row["logon_type"]=(int)info.LogonType; row["logon_session_id"]=(int)info.Session;
                row["logon_sid"]=Sid(info.Sid); row["logon_time_filetime"]=info.LogonTime;
            } finally { LsaFreeReturnBuffer(logon); }
        } catch(Exception exc) { errors.Add(Error(exc)); }
        return row;
    }
    static string SessionText(int session,int kind) {
        IntPtr data; int bytes;
        if(!WTSQuerySessionInformationW(IntPtr.Zero,session,kind,out data,out bytes)) throw new Win32Exception(Marshal.GetLastWin32Error());
        try { return Marshal.PtrToStringUni(data) ?? ""; } finally { WTSFreeMemory(data); }
    }
    public static Dictionary<string,object> Capture() {
        var result=new Dictionary<string,object>(); var errors=new List<string>(); result["errors"]=errors;
        result["current"]=null; result["linked"]=null; result["linked_error"]=null;
        result["active_console_session_id"]=WTSGetActiveConsoleSessionId();
        result["observer_pid"]=Environment.ProcessId;
        using(var process=Process.GetCurrentProcess()) result["observer_creation_filetime"]=process.StartTime.ToUniversalTime().ToFileTimeUtc();
        IntPtr token;
        if(OpenProcessToken(GetCurrentProcess(),8,out token)) {
            try {
                result["current"]=ReadToken(token);
                try {
                    IntPtr data=Info(token,TokenLinkedToken), linked;
                    try { linked=Marshal.ReadIntPtr(data); } finally { Marshal.FreeHGlobal(data); }
                    try { result["linked"]=ReadToken(linked); } finally { CloseHandle(linked); }
                } catch(Exception exc) { result["linked_error"]=Error(exc); }
            } finally { CloseHandle(token); }
        } else { errors.Add("OpenProcessToken:"+Marshal.GetLastWin32Error()); }
        var sessions=new List<Dictionary<string,object>>(); result["sessions"]=sessions;
        IntPtr buffer; int count;
        if(!WTSEnumerateSessionsW(IntPtr.Zero,0,1,out buffer,out count)) errors.Add("WTSEnumerateSessionsW:"+Marshal.GetLastWin32Error());
        else {
            try {
                for(int i=0;i<count;i++) {
                    var info=Marshal.PtrToStructure<WTS_SESSION>(IntPtr.Add(buffer,i*Marshal.SizeOf<WTS_SESSION>()));
                    var row=new Dictionary<string,object>{{"session_id",info.Id},{"state",info.State},{"sid",null},{"error",null}};
                    try {
                        string user=SessionText(info.Id,5), domain=SessionText(info.Id,7);
                        if(user.Length>0) row["sid"]=((SecurityIdentifier)new NTAccount(domain,user).Translate(typeof(SecurityIdentifier))).Value;
                    } catch(Exception exc) { row["error"]=Error(exc); }
                    sessions.Add(row);
                }
            } finally { WTSFreeMemory(buffer); }
        }
        return result;
    }
}
