"""CI-only safe native directory ACL diagnosis; no product/profile writes."""
import json, os, tempfile
from pathlib import Path
import win32security as s
import win32api, win32con
flags=s.OWNER_SECURITY_INFORMATION|s.DACL_SECURITY_INFORMATION
token=s.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
user=s.ConvertSidToStringSid(s.GetTokenInformation(token,s.TokenUser)[0])
logon=[s.ConvertSidToStringSid(sid) for sid,attrs in s.GetTokenInformation(token,s.TokenGroups) if attrs & 0xC0000000 == 0xC0000000]
rows=[]
paths=set()
for root in (Path(tempfile.gettempdir()),Path(os.environ['USERPROFILE']),Path.cwd()):
    paths.update([root,*root.parents])
for path in sorted(paths,key=str):
    sd=s.GetNamedSecurityInfo(str(path),s.SE_FILE_OBJECT,flags)
    rows.append({'path':str(path),'security':s.ConvertSecurityDescriptorToStringSecurityDescriptor(sd,1,flags)})
out=Path('../evidence');out.mkdir(exist_ok=True)
(out/'native-directory-acls.json').write_text(json.dumps({'user':user,'logon':logon,'directories':rows},indent=2),encoding='utf-8')
print(json.dumps({'user':user,'logon':logon,'directories':rows}))
