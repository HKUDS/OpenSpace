"""Versioned source reading/navigation over immutable portfolio artifacts."""
from dataclasses import replace
from pathlib import PurePosixPath

from agentpilot.domain import canonical_json
from agentpilot.research.models import ResearchContentUnavailable
from agentpilot.app.model_harnesses import build_direction_alias_catalog
from agentpilot.policy.proposer.context_views import render_program_flow
from .repository_map import ImmutableGitDefinitionReader
from .structural_adherence import PythonParentRepositoryMapBuilder


class ArtifactSourceReader:
    def __init__(self, objects, store, roots, *, request=None, editable_roots=None,
                 definition_reader=None, max_files=4096):
        self.objects, self.store, self.request = objects, store, request
        self.roots = tuple(sorted(set(p.rstrip('/') for p in roots)))
        self.reader = definition_reader or ImmutableGitDefinitionReader(objects)
        self.builder = PythonParentRepositoryMapBuilder(objects, editable_roots=tuple(sorted(set(editable_roots or self.roots))), max_files=max_files)
        self.requests = {}

    @staticmethod
    def _path(path):
        if not isinstance(path, str) or not path:
            raise ValueError('Source path must be nonempty text')
        p = PurePosixPath(path)
        if p.is_absolute() or p.as_posix() != path or '..' in p.parts or '\\' in path or any(ord(c) < 32 for c in path):
            raise ValueError('Source path must be normalized and repository-relative')
        return path

    def text(self, artifact, path):
        path = self._path(path)
        if not any(path == root or path.startswith(root + '/') for root in self.roots):
            raise ValueError('Source path must be inside visible program roots')
        content = self.objects.read_regular_blob(artifact.commit_sha, path)
        if content is None:
            raise ValueError('No regular source file exists at this artifact path')
        return content.decode('utf-8')

    def read(self, artifact, path, *, line=1, count=120):
        if type(line) is not int or line < 1 or type(count) is not int or not 1 <= count <= 200:
            raise ValueError('line starts at 1; count must be 1..200')
        version = self.store.artifact(artifact)
        lines = self.text(version, path).splitlines()
        start, end = line - 1, min(len(lines), line - 1 + count)
        text = '\n'.join(f'{i+1}: {lines[i]}' for i in range(start, end))
        if len(text.encode()) > 24576:
            raise ValueError('Requested source is too large; reduce count')
        result = {'artifact': artifact, 'commit_sha': version.commit_sha, 'path': path,
                  'line': line, 'content': text, 'total_lines': len(lines)}
        if end < len(lines):
            result['next'] = {'tool': 'read', 'arguments': {'artifact': artifact, 'path': path, 'line': end+1, 'count': count}}
        return result

    def _request(self, artifact):
        if self.request is None:
            raise ValueError('Versioned navigation requires the source context request')
        if artifact.id not in self.requests:
            mapping = self.builder.build(parent_id=artifact.id, commit_sha=artifact.commit_sha)
            parent = replace(self.request.parent_selection, parent_id=artifact.id, commit_sha=artifact.commit_sha)
            self.requests[artifact.id] = replace(self.request, parent_selection=parent, repository_map=mapping)
        request = self.requests[artifact.id]
        if request.repository_map.commit_sha != artifact.commit_sha:
            raise ValueError('Artifact source identity changed')
        return request

    def navigate(self, tool, args, *, max_bytes=16384):
        artifact = self.store.artifact(args['artifact'])
        request = self._request(artifact)
        if tool == 'search':
            return self._search(artifact, request, args, max_bytes)
        if tool != 'list_sources' or set(args) - {'artifact', 'path', 'cursor'}:
            raise ValueError('Versioned list_sources accepts artifact, path and cursor')
        path = args.get('path')
        cursor = 0 if args.get('cursor') is None else args['cursor']
        if type(cursor) is not int or cursor < 0:
            raise ValueError('Use the returned directory cursor')
        files = {file.path: file for file in request.repository_map.files}
        visible = self.reader._visible_source_paths(request)
        if path is None:
            rows = [{'path': root, 'kind': 'directory'} for root in self.roots]
        elif self._path(path) in files:
            rows = [{'name': symbol.name, 'line': symbol.first_line,
                     'open': {'tool': 'read', 'arguments': {'artifact': artifact.id, 'path': path, 'line': symbol.first_line}}}
                    for symbol in files[path].symbols]
        elif path in visible:
            rows = [{'path': path, 'kind': 'file'}]
        else:
            children = {}
            for file in visible:
                if file.startswith(path + '/'):
                    tail = file[len(path)+1:]
                    child = path + '/' + tail.split('/', 1)[0]
                    children[child] = 'directory' if '/' in tail else 'file'
            if not children:
                raise ValueError('Directory is outside this artifact source inventory')
            rows = [{'path': p, 'kind': kind} for p, kind in sorted(children.items())]
        for row in rows:
            if 'open' not in row:
                row['open'] = {'tool': 'read' if row['kind']=='file' else 'list_sources',
                               'arguments': {'artifact': artifact.id, 'path': row['path']}}
        if cursor > len(rows):
            raise ValueError('Directory cursor is outside this view')
        end = min(len(rows), cursor + 16)
        result = {'artifact': artifact.id, 'commit_sha': artifact.commit_sha, 'path': path, 'entries': rows[cursor:end], 'total': len(rows)}
        if path is None and cursor == 0 and max_bytes >= 4096:
            try:
                flow = self.reader.program_flow_context(request=request, max_bytes=min(8192, max_bytes//2))
                refs = {node.ref: f"{artifact.id}:{node.file}:{node.lines[0]}" for node in build_direction_alias_catalog(request).targets}
                def versioned(value):
                    if isinstance(value, str): return refs.get(value, value)
                    if isinstance(value, list): return [versioned(v) for v in value]
                    if isinstance(value, dict):
                        result = {k: versioned(v) for k, v in value.items()}
                        if result.get('tool') in {'read', 'search', 'list_sources'}:
                            result['arguments'] = {**result['arguments'], 'artifact': artifact.id}
                        return result
                    return value
                result['structure'] = render_program_flow(versioned(flow))
            except ValueError as error:
                result['structure_note'] = f'Structure view unavailable: {error}; use the file directory.'
        while len(canonical_json(result).encode()) > max_bytes - 256:
            if 'structure' in result:
                result.pop('structure')
                result['structure_note'] = 'Use the file directory to inspect the structure in smaller sections.'
            elif result['entries']:
                result['entries'].pop(); end -= 1
            else:
                raise ResearchContentUnavailable('Source navigation cannot fit this allowance')
        if end < len(rows):
            if end == cursor:
                raise ResearchContentUnavailable('Source navigation cannot fit an entry in this allowance')
            result['next'] = {'tool': tool, 'arguments': {**args, 'cursor': end}}
        return result

    def _search(self, artifact, request, args, max_bytes):
        import json
        if set(args) - {'artifact', 'query', 'path', 'scope', 'cursor'} or args.get('scope', 'source') != 'source':
            raise ValueError('artifact applies only to source search')
        identity = {'artifact': artifact.id, 'commit': artifact.commit_sha, 'query': args.get('query'), 'path': args.get('path')}
        cursor = None
        if args.get('cursor') is not None:
            if not isinstance(args['cursor'], str) or len(args['cursor']) > 4096:
                raise ValueError('Use the returned search cursor')
            saved = json.loads(args['cursor'])
            if not isinstance(saved, dict) or saved.get('identity') != identity:
                raise ValueError('Search cursor belongs to another artifact or query')
            cursor = saved['position']
        result = self.reader.search(request=request, query=args.get('query'), path=args.get('path'), cursor=cursor,
                                    max_results=12, max_bytes=max(512, max_bytes-1024))
        position = result.pop('next_cursor')
        result.pop('parent_commit', None)
        result.update(artifact=artifact.id, commit_sha=artifact.commit_sha, scope='source')
        for row in result['matches']:
            row['open'] = {'tool': 'read', 'arguments': {'artifact': artifact.id, 'path': row['path'], 'line': row['line']}}
        if position is not None:
            token = canonical_json({'identity': identity, 'position': position})
            result['next'] = {'tool': 'search', 'arguments': {**args, 'cursor': token}}
        return result
