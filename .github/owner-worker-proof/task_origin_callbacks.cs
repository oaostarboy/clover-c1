// Fixture-only SDK callback sink. No event-id -> action-PID schema adapter.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Diagnostics.Eventing.Reader;
using System.Globalization;
using System.Xml;

public sealed class TaskOriginCallback {
    public long Sequence;
    public string Channel;
    public string Provider;
    public int EventId;
    public int Version;
    public string[] Fields;
    public string Xml;
    public bool OriginalXml;
    public long ObservedUtcTicks;
    public int LivePid;
    public long LiveCreationFileTime;
    public string LiveImage;
    public string LiveIdentityError;
}

public sealed class TaskOriginCallbacks : IDisposable {
    readonly object gate = new object();
    readonly List<TaskOriginCallback> records = new List<TaskOriginCallback>();
    readonly List<string> errors = new List<string>();
    readonly List<EventLogWatcher> watchers = new List<EventLogWatcher>();
    readonly string task;
    readonly string[] images;
    readonly string[] commands;
    long sequence;
    int dropped;
    bool disposed;
    public TaskOriginCallbacks(string taskPath, string[] expectedImages, string[] safeCommands) {
        task = taskPath; images = expectedImages; commands = safeCommands;
    }
    public TaskOriginCallback[] Records { get { lock (gate) return records.ToArray(); } }
    public string[] Errors { get { lock (gate) return errors.ToArray(); } }
    public int Dropped { get { lock (gate) return dropped; } }
    public void Subscribe() {
        // EventLogWatcher(query) is future events only (no read-existing mode).
        // No audit/log policy is changed. A missing/disabled channel is a blocker.
        foreach (string channel in new string[] {"Microsoft-Windows-TaskScheduler/Operational", "Security"}) {
            var query = new EventLogQuery(channel, PathType.LogName,
                channel == "Security" ? "*[System[EventID=4688]]" : "*");
            var watcher = new EventLogWatcher(query);
            watcher.EventRecordWritten += OnEvent;
            watchers.Add(watcher);
            watcher.Enabled = true;
        }
    }
    void OnEvent(object sender, EventRecordWrittenEventArgs args) {
        try {
            if (args.EventException != null) {
                lock (gate) errors.Add(args.EventException.GetType().Name);
                return;
            }
            using (EventRecord record = args.EventRecord) {
                if (record == null) throw new InvalidOperationException("empty SDK callback");
                OnXml(record.ToXml());
            }
        } catch (Exception exc) { lock (gate) errors.Add(exc.GetType().Name); }
    }
    public void OnXml(string xml) {
        // This exact sink also runs against explicitly modeled XML on Linux.
        // That replay cannot enable Subscribe() or native acceptance.
        lock (gate) {
            if (disposed) { errors.Add("callback-after-dispose"); return; }
            try {
                var doc = new XmlDocument(); doc.XmlResolver = null; doc.LoadXml(xml);
                var ns = new XmlNamespaceManager(doc.NameTable);
                ns.AddNamespace("e", "http://schemas.microsoft.com/win/2004/08/events/event");
                var fields = new Dictionary<string, string>();
                var names = new List<string>();
                foreach (XmlNode node in doc.SelectNodes("/e:Event/e:EventData/e:Data", ns)) {
                    string name = node.Attributes["Name"].Value;
                    if (fields.ContainsKey(name)) throw new InvalidOperationException("duplicate-field");
                    fields.Add(name, node.InnerText); names.Add(name);
                }
                string channel = doc.SelectSingleNode("/e:Event/e:System/e:Channel", ns).InnerText;
                bool taskEvent = channel == "Microsoft-Windows-TaskScheduler/Operational";
                if (taskEvent) {
                    if (!fields.ContainsKey("TaskName") || fields["TaskName"] != task) return;
                } else if (channel == "Security") {
                    if (!fields.ContainsKey("NewProcessName") || !Contains(images, fields["NewProcessName"])) return;
                } else throw new InvalidOperationException("unexpected-channel");
                if (records.Count >= 256) { dropped++; return; }
                bool original = true;
                // Retain exact raw XML only for bounded, non-secret values.
                // Unexpected actions/argv/unknown payload fields are diagnostic
                // schema-only evidence and can NEVER be a complete witness.
                foreach (XmlNode node in doc.SelectNodes("/e:Event/e:EventData/e:Data", ns)) {
                    if (!SafeValue(node.Attributes["Name"].Value, node.InnerText, taskEvent)) {
                        node.InnerText = "REDACTED_UNEXPECTED_PAYLOAD";
                        original = false;
                    }
                }
                var item = new TaskOriginCallback();
                item.Sequence = ++sequence; item.Channel = channel;
                item.Provider = doc.SelectSingleNode("/e:Event/e:System/e:Provider", ns).Attributes["Name"].Value;
                item.EventId = Int32.Parse(doc.SelectSingleNode("/e:Event/e:System/e:EventID", ns).InnerText);
                item.Version = Int32.Parse(doc.SelectSingleNode("/e:Event/e:System/e:Version", ns).InnerText);
                item.Fields = names.ToArray(); item.Xml = original ? xml : doc.OuterXml;
                item.OriginalXml = original; item.ObservedUtcTicks = DateTime.UtcNow.Ticks;
                // This is a separately sampled LIVE identity, NOT history inferred
                // from a PPID, EnginePID, LastRunTime, or event timestamp.
                if (!taskEvent && fields.ContainsKey("NewProcessId")) {
                    try {
                        string rawPid = fields["NewProcessId"];
                        int pid = rawPid.StartsWith("0x", StringComparison.OrdinalIgnoreCase)
                            ? Int32.Parse(rawPid.Substring(2), NumberStyles.HexNumber) : Int32.Parse(rawPid);
                        using (var process = Process.GetProcessById(pid)) {
                            item.LivePid = pid;
                            item.LiveCreationFileTime = process.StartTime.ToUniversalTime().ToFileTimeUtc();
                            item.LiveImage = process.MainModule.FileName;
                        }
                    } catch (Exception exc) { item.LiveIdentityError = exc.GetType().Name; }
                }
                records.Add(item);
            } catch (Exception exc) { errors.Add(exc.GetType().Name); }
        }
    }
    bool SafeValue(string name, string value, bool taskEvent) {
        if (taskEvent && name == "TaskName") return value == task;
        if (name == "ActionName") return Contains(images, value) || value == "wscript.exe";
        if (name == "NewProcessName" || name == "ParentProcessName") return Contains(images, value);
        if (name == "CommandLine") return Contains(commands, value);
        Guid guid;
        if (name == "InstanceId" || name == "TaskInstanceId") return Guid.TryParse(value, out guid);
        if (name == "SubjectUserSid" || name == "TargetUserSid" || name == "UserContext" || name == "MandatoryLabel")
            return System.Text.RegularExpressions.Regex.IsMatch(value, @"^S-1-[0-9]+(?:-[0-9]+)+$");
        if (name == "ProcessId" || name == "ProcessID" || name == "NewProcessId" || name == "EnginePID" ||
            name == "ResultCode" || name == "ErrorValue" || name == "SubjectLogonId" || name == "TargetLogonId")
            return System.Text.RegularExpressions.Regex.IsMatch(value, @"^(?:0x[0-9a-fA-F]+|[0-9]+)$");
        return false;
    }
    static bool Contains(string[] values, string value) {
        foreach (string candidate in values) if (String.Equals(candidate, value, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }
    public void Dispose() {
        // Do not hold gate while waiting for SDK callback completion.
        foreach (var watcher in watchers) { watcher.Enabled = false; watcher.Dispose(); }
        watchers.Clear();
        lock (gate) disposed = true;
    }
}
