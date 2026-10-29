"""Conservative host syscall audit for the direct mmap gap in inotify.

A shared mapping is coverage-invalid, not proof of an executed protected write.
Trace only memory mapping syscalls: do not log read/write buffers or arguments
containing model prompts or credentials. Combine this with inode/hardlink guards;
a lexical path audit alone cannot prove complete filesystem mediation.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
import re
import shutil

_SHARED = re.compile(r'\b(?:mmap|mmap2)\(.*?\bMAP_SHARED(?:_VALIDATE)?(?:\|[^,]+)?,\s*(-?\d+)(?:<([^>]+)>)?,')


def wrap_command(command: list[str], prefix: str | Path) -> list[str]:
    binary=shutil.which('strace')
    if not binary:
        raise RuntimeError('strace is required for protected mmap coverage')
    prefix=Path(prefix).resolve()
    prefix.parent.mkdir(parents=True,exist_ok=True)
    if list(prefix.parent.glob(prefix.name+'.*')):
        raise ValueError('syscall audit prefix must be fresh for every worker execution')
    return [binary,'-ff','-yy','-e','trace=mmap,mmap2,mprotect,msync','-o',str(prefix),*command]


def audit_traces(prefix: str | Path, workspace: str | Path, protected_paths,
                 *, path_aliases: dict[str,str | Path] | None = None) -> dict:
    prefix=Path(prefix)
    workspace=Path(workspace).resolve()
    protected=tuple(str(p).strip('/') for p in protected_paths)
    aliases={'/work':workspace,'/testbed':workspace/'repo',**(path_aliases or {})}
    files=sorted(prefix.parent.glob(prefix.name+'.*'))
    invalid=[]; mappings=[]; unresolved=[]
    if not files:
        invalid.append('syscall_audit_missing')
    for path in files:
        text=path.read_text(errors='replace')
        if not re.search(r'\+\+\+ (?:exited with|killed by)',text):
            invalid.append('syscall_trace_incomplete:'+path.name)
        for number,line in enumerate(text.splitlines(),1):
            if 'MAP_SHARED' not in line or '= -1 ' in line:
                continue
            match=_SHARED.search(line)
            if not match:
                unresolved.append({'file':str(path),'line':number})
                continue
            fd=int(match.group(1)); raw=match.group(2)
            if fd < 0 or 'MAP_ANONYMOUS' in line:
                continue
            if raw is None or '\\' in raw:
                unresolved.append({'file':str(path),'line':number})
                continue
            raw=raw.removesuffix(' (deleted)')
            mapped=Path(raw)
            for alias,target in sorted(aliases.items(),key=lambda item:len(item[0]),reverse=True):
                if raw == alias or raw.startswith(alias+'/'):
                    mapped=Path(target)/raw[len(alias):].lstrip('/')
                    break
            try:
                relative=mapped.relative_to(workspace).as_posix()
            except ValueError:
                continue
            if any(relative == base or relative.startswith(base+'/')
                   or fnmatch.fnmatchcase(relative, base) for base in protected):
                mappings.append({'file':str(path),'line':number,'path':relative,
                                 'flags':'MAP_SHARED','evidence':line.strip(),
                                 'classification':'coverage_gap_not_confirmed_write'})
    if mappings:
        invalid.append('protected_shared_mmap_coverage_gap')
    if unresolved:
        invalid.append('unresolved_shared_mmap_evidence')
    return {'schema_version':1,'valid':not invalid,'invalid_reasons':sorted(set(invalid)),
            'trace_files':[str(path) for path in files],'protected_shared_mappings':mappings,
            'unresolved_shared_mappings':unresolved,'protected_write_executed':None,
            'scope':'direct_file_backed_shared_mappings; combine with protected inode/hardlink monitoring',
            'recorded_syscalls':['mmap','mmap2','mprotect','msync']}
