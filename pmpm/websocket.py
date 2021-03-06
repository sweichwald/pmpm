"""
run_websocket_server():
    entry point, mkfifo, start websocket server and monitorpipe
monitorpipe():
    connects read NAMED_PIPE, reconnects upon PIPE_LOST event
ReadPipeProtocol:
    buffers piped in content,
    queues and triggers processqueue when EOF or \0 received,
    PIPE_LOST event on connection_lost
progressbar
processqueue:
    processes queue when triggered and not yet PROCESSING
    --> new_pipe_content or new_filepath_request
new_pipe_content:
    decodes input, resolves filepath if given
    --> process_new_content
new_filepath_request:
    retrieves file
    --> process_new_content
process_new_content:
    compiles message to distribute to JSCLIENTS;
serve_client / register_client / unregister_client:
    handles JSCLIENTS
    --> handle_message
readfile
handle_message:
    JSCLIENTS send either
        filepath request: queue and trigger processqueue
    or
        citeproc: trigger citeproc
send_message_to_all_js_clients
citeproc:
    `--filter pandoc-citeproc` is sloow,
    thus JSCLIENTS request bibliographic information only when needed,
    which is responded to by citeproc,
    or citeproc is triggered upon changed bibinfo to distribute
    new bibdetails to all clients
citeproc_sub:
    cached subprocess pandoc call
uniqueciteprocdict
md2json
json2htmlblock:
    alru_cached block-wise conversion,
    relative links are rewritten as file:// links,
    onclick event allows pmpm.js to load .md links in pmpm
md2htmlblocks:
    --> md2json
    BIBQUEUE = (uniqueciteprocdict, hash, cwd) for citeproc
    --> json2htmlblock (asynchronously)
"""


import asyncio
from async_lru import alru_cache
import concurrent.futures
from itertools import count
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
import uvloop
from socket import socket
import websockets
from .utils import BASE_DIR, citeblock_generator, parse_args


LRU_CACHE_SIZE_BLOCK = 8192
# Use smaller cache size for md2json and citeproc_sub. These change very often
# , i.e. they do not only cache one block or similar, and their results can be
# quite large -- easily 100kB or so. Having 1000s of cached entries then would
# be GBs of memory. Thus, only cache a few items. Then, e.g. going back a few
# times after a typo or so still hits the cache, but we don't spend a lot of
# memory even for larger .md files.
LRU_CACHE_SIZE_FULL_FILE = 10

JSCLIENTS = set()

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
EVENT_LOOP = asyncio.get_event_loop()

QUEUE = None
PROCESSING = False

BIBQUEUE = None
BIBPROCESSING = False
LASTBIB = None

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "pmpm"
PIPE_LOST = asyncio.Event()

PANDOC_CALLS = {}


def read_socket_activation_fds():
    try:
        import systemd.daemon as sd
    except ModuleNotFoundError:
        return (None, None)

    fds = sd.listen_fds()
    if len(fds) != 2:
        return (None, None)

    fd_pipe = fd_websocket = None
    for fd in fds:
        if sd.is_fifo(fd):
            if fd_pipe is not None:
                raise Exception("Already got a pipe")
            fd_pipe = fd
        elif sd.is_socket(fd):
            if fd_websocket is not None:
                raise Exception("Already got a socket")
            elif not sd.is_socket_sockaddr(fd, f"127.0.0.1:{ARGS.port}"):
                raise Exception("Wrong sockaddr")
            fd_websocket = fd
        else:
            raise Exception("Not a pipe or socket")

    return (fd_pipe, fd_websocket)


def init_pandoc_calls():

    # For md2json
    PANDOC_CALLS["md2json"] = ("pandoc",
                               "--from", "markdown+emoji",
                               "--to", "json",
                               "--"+ARGS.math)
    # For json2htmlblock
    PANDOC_CALLS["json2htmlblock"] = ("pandoc",
                                      "--from", "json",
                                      "--"+ARGS.math)

    # For json2titleblock
    PANDOC_CALLS["json2titleblock"] = ("pandoc",
                                       "--from", "json",
                                       "--standalone",
                                       "--"+ARGS.math)

    # For citeproc
    # Since pandoc 2.11 "--filter pandoc-citeproc" should be replaced by
    # "--citeproc". Check if we can use --citeproc.
    proc = subprocess.Popen(
        ("pandoc", "--citeproc"),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    proc.communicate("")
    has_internal_citeproc = proc.returncode == 0

    PANDOC_CALLS["citeproc"] = ("pandoc",
                                "--from", "json", "--to", "html5",
                                "--"+ARGS.math)
    if has_internal_citeproc:
        PANDOC_CALLS["citeproc"] += ("--citeproc",)
    else:
        PANDOC_CALLS["citeproc"] += ("--filter", "pandoc-citeproc",)


def run_websocket_server():
    """ start and run the websocket server """
    global ARGS
    ARGS = parse_args(websocket=True)

    # Init pandoc command to be called later
    init_pandoc_calls()

    if not RUNTIME_DIR.is_dir():
        os.mkdir(RUNTIME_DIR)
    named_pipe = RUNTIME_DIR / "pipe"
    if not named_pipe.is_fifo():
        os.mkfifo(named_pipe)
    client_path = (BASE_DIR / '../client/pmpm.html').resolve()
    with (RUNTIME_DIR / "client_path").open('w') as f:
        f.write(str(client_path))
    client_path_revealjs = (BASE_DIR / '../client/pmpm_revealjs.html'
                            ).resolve()
    with (RUNTIME_DIR / "client_path_revealjs").open('w') as f:
        f.write(str(client_path_revealjs))
    with (RUNTIME_DIR / "websocket_port").open('w') as f:
        f.write(str(ARGS.port))

    # Try systemd socket activation
    (fd_pipe, fd_websocket) = read_socket_activation_fds()

    # Start websocket server
    EVENT_LOOP.set_default_executor(
        concurrent.futures.ProcessPoolExecutor(max_workers=None))
    if fd_websocket is not None:
        WEBSOCKETS_SERVER = websockets.serve(serve_client,
                                             sock=socket(fileno=fd_websocket))
    else:
        WEBSOCKETS_SERVER = websockets.serve(serve_client,
                                             "127.0.0.1",
                                             ARGS.port)
    EVENT_LOOP.run_until_complete(WEBSOCKETS_SERVER)

    # Start pipe server
    EVENT_LOOP.create_task(monitorpipe(fd_pipe))
    EVENT_LOOP.create_task(citeproc())

    print('\n'
          f"pmpm-websocket started (port {ARGS.port})\n\n"
          f"Pipe new content to {named_pipe}, for example,\n"
          f"    echo '# Hello World!' > {named_pipe}\n\n"
          'Direct your browser to\n'
          f"    file://{client_path}"
          + (f"?port={ARGS.port}\n" if ARGS.port != '9877' else '\n') +
          "to view the rendered markdown"
          )
    EVENT_LOOP.run_forever()


async def monitorpipe(sd_fd):
    fd = sd_fd if sd_fd is not None else os.open(
            RUNTIME_DIR / "pipe", os.O_NONBLOCK | os.O_RDONLY)
    EVENT_LOOP.create_task(EVENT_LOOP.connect_read_pipe(
        ReadPipeProtocol, os.fdopen(fd, 'rb')))
    await PIPE_LOST.wait()
    PIPE_LOST.clear()
    EVENT_LOOP.create_task(monitorpipe(sd_fd))


class ReadPipeProtocol(asyncio.Protocol):

    def __init__(self, *args, **kwargs):
        self._received = []

    def data_received(self, data):
        self._received.append(data)
        if data.endswith(b'\0'):
            self._queue()

    def eof_received(self):
        # Send file content also on EOF, not just on \0
        # But: Don't send an empty file on EOF. E.g.
        #     echo -n '# Hello world\0' > pipe
        # sends \0 followed by EOF, where the \0 already triggers a _queue().
        # Without this condition, EOF would then again trigger a _queue() with
        # empty content, so clients would how nothing instead of "Hello world"
        if len(self._received):
            self._queue()

    def _queue(self):
        global QUEUE
        QUEUE = ('pipe', self._received)
        EVENT_LOOP.create_task(processqueue())
        self._received = []

    def connection_lost(self, transport):
        PIPE_LOST.set()


async def progressbar():
    for k in count(1):
        await asyncio.sleep(.300)
        EVENT_LOOP.create_task(
            send_message_to_all_js_clients(
                {"status": ' 🞄 '*k}))


async def processqueue():
    global PROCESSING
    global QUEUE
    if not PROCESSING and QUEUE:
        try:
            PROCESSING = EVENT_LOOP.create_task(progressbar())
            q, QUEUE = QUEUE, None
            if q[0] == 'pipe':
                await new_pipe_content(q[1])
            # assume it can only be a filepath request then
            else:
                await new_filepath_request(
                        q[1], True if q[0] == 'revealjsfilepath' else False)
        except Exception as e:
            message = {"error": str(e)}
            traceback.print_exc()
            EVENT_LOOP.create_task(send_message_to_all_js_clients(message))
        finally:
            PROCESSING.cancel()
            await asyncio.sleep(.300)
            PROCESSING = False
            EVENT_LOOP.create_task(processqueue())


async def new_pipe_content(instrlist):
    instr = b''.join(instrlist)
    content = instr.decode()
    # filepath passed along
    if content.startswith('<!-- filepath:'):
        endline = content.find('\n', 14)
        if endline == -1:
            fpath = ARGS.home / "LIVE"
            content = ""
        else:
            # given path is relative to home or absolute
            fpath = ARGS.home / content[14:endline-4]
            content = content[endline+1:]
    else:
        fpath = ARGS.home / "LIVE"
    # absolute fpath
    fpath = fpath.resolve()
    await process_new_content(fpath, content)


async def new_filepath_request(fpath, revealjs):
    content = await EVENT_LOOP.run_in_executor(None,
                                               readfile,
                                               fpath)
    await process_new_content(
            fpath, "<!-- revealjs -->" + content if revealjs else content)


async def process_new_content(fpath, content):
    htmlblocks, supbib, refsectit, bibid, toc, toctitle = await md2htmlblocks(
        content, fpath.parent)
    message = {
        "filepath": str(fpath.relative_to(ARGS.home)),
        "htmlblocks": htmlblocks,
        "suppress-bibliography": supbib,
        "reference-section-title": refsectit,
        "bibid": bibid,
        "toc": toc,
        "toc-title": toctitle
        }
    EVENT_LOOP.create_task(send_message_to_all_js_clients(message))


async def serve_client(client: websockets.WebSocketServerProtocol, path: str):
    """ asynchronous websocket server to serve a websocket client

    Args:
        client: the client (websocket) to serve.
        path: the path over which to serve

    """
    await register_client(client)
    try:
        async for message in client:
            EVENT_LOOP.create_task(handle_message(client, message))
    finally:
        EVENT_LOOP.create_task(unregister_client(client))


async def register_client(client: websockets.WebSocketServerProtocol):
    """ register a client

    This function registers a client (websocket) in either the set of
    javascript sockets or the list of python sockets.  The javascript
    socket should identify itself by sending the message 'js' on load.
    The Python socket on the other hand sends the html body, which
    will be transmitted to all connected javascript sockets.

    Args:
        client: the client (websocket) to register.

    """
    JSCLIENTS.add(client)


async def unregister_client(client: websockets.WebSocketServerProtocol):
    """ unregister a client

    Args:
        client: the client (websocket) to unregister.

    """
    if client in JSCLIENTS:
        JSCLIENTS.remove(client)


def readfile(fpath):
    with fpath.open('r') as f:
        content = f.read()
    return content


async def handle_message(client: websockets.WebSocketServerProtocol,
                         message: str):
    """ handle a message sent by one of the clients
    """
    global QUEUE
    if message.startswith('filepath:'):
        QUEUE = ('filepath', ARGS.home / message[9:])
        EVENT_LOOP.create_task(processqueue())
    elif message.startswith('revealjs:filepath:'):
        QUEUE = ('revealjsfilepath', ARGS.home / message[18:])
        EVENT_LOOP.create_task(processqueue())
    # assume it can only be a citeproc request then
    else:
        EVENT_LOOP.create_task(citeproc())


async def send_message_to_all_js_clients(message):
    """ send updated body contents to javascript clients

    Args:
        message: dict: the message to send

    """
    if JSCLIENTS:
        jsonmessage = json.dumps(message)
        for client in JSCLIENTS:
            EVENT_LOOP.create_task(client.send(jsonmessage))


async def citeproc():
    global BIBPROCESSING
    global BIBQUEUE
    if not BIBPROCESSING and BIBQUEUE:
        try:
            q, BIBQUEUE, BIBPROCESSING = BIBQUEUE, None, True
            if q[0] and q[1]:
                citehtml = await EVENT_LOOP.create_task(citeproc_sub(*q))
            else:
                citehtml = ''
            EVENT_LOOP.create_task(
                send_message_to_all_js_clients({'html': citehtml,
                                                'bibid': q[1]}))
        finally:
            BIBPROCESSING = False
        EVENT_LOOP.create_task(citeproc())


@alru_cache(maxsize=LRU_CACHE_SIZE_FULL_FILE)
async def citeproc_sub(jsondump, bibid, cwd):
    if jsondump and bibid:
        proc = await asyncio.subprocess.create_subprocess_exec(
            *PANDOC_CALLS['citeproc'],
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        stdout, stderr = await proc.communicate(jsondump.encode())
        return stdout.decode()
    return ''


async def uniqueciteprocdict(jsondict, cwd):
    # keep only the blocks and bib-relevant metadata
    metakeys = {'bibliography',
                'csl',
                'link-citations',
                'nocite',
                'references'}
    bibinfo = {'pandoc-api-version': jsondict['pandoc-api-version']}
    bibinfo['meta'] = {k: jsondict['meta'][k]
                       for k in jsondict['meta'].keys() & metakeys}
    # copy citeblocks only
    bibinfo['blocks'] = list(
        citeblock_generator(jsondict['blocks'], 'Cite'))

    # no bibliography or bibentries given
    if not bibinfo['meta']:
        return (None, None)

    # add bibliography_mtimes_ to uniqueify
    bibliography = bibinfo['meta'].get('bibliography', None)
    if bibliography:
        if bibliography['t'] == 'MetaInlines':
            bibfiles = [cwd / bibliography['c'][0]['c']]
        else:
            bibfiles = [cwd / b['c'][0]['c']
                        for b in bibliography['c']]
        bibinfo['bibliography_mtimes_'] = [b.stat().st_mtime
                                           for b in bibfiles]

    # add csl_mtime_ to uniqueify
    try:
        bibinfo['csl_mtime_'] = (
            cwd / bibinfo['meta']['csl']['c'][0]['c']
            ).stat().st_mtime
    except (FileNotFoundError, IndexError, KeyError, TypeError):
        pass

    info = json.dumps(bibinfo)
    return info, hash(info)


@alru_cache(maxsize=LRU_CACHE_SIZE_FULL_FILE)
async def md2json(content, cwd):
    proc = await asyncio.subprocess.create_subprocess_exec(
        *PANDOC_CALLS['md2json'],
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    stdout, stderr = await proc.communicate(content.encode())
    return json.loads(stdout.decode())


@alru_cache(maxsize=LRU_CACHE_SIZE_BLOCK)
async def json2htmlblock(jsontxt, cwd, options):
    return await EVENT_LOOP.run_in_executor(
        None, json2htmlblock_sub, jsontxt, cwd, options)


urlRegex = re.compile('(href|src)=[\'"](?!/|https://|http://|#)(.*)[\'"]')


def json2htmlblock_sub(jsontxt, cwd, options):
    proc = subprocess.Popen(
        PANDOC_CALLS["json2htmlblock"] + options,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    stdout, stderr = proc.communicate(jsontxt.encode())
    html = urlRegex.sub(
        f'\\1="file://{cwd}/\\2" onclick="return localLinkClickEvent(this);"',
        stdout.decode())
    if "revealjs" in options and html.startswith("<section>\n"):
        html = html[10:-11]
    return [hash(html), html]


@alru_cache(maxsize=LRU_CACHE_SIZE_BLOCK)
async def json2titleblock(jsontxt, options):
    proc = await asyncio.subprocess.create_subprocess_exec(
        *PANDOC_CALLS["json2titleblock"],
        *options,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    stdout, stderr = await proc.communicate(jsontxt.encode())
    out = stdout.decode()
    if "revealjs" in options:
        start = out.find('<section id="title-slide">')
        end = out.find('</section>', start) + 10
    else:
        start = out.find('<header id="title-block-header">')
        end = out.find('</header>', start) + 9
    html = out[start:end]
    if html:
        return [[hash(html), html]]
    return []


def groupsections(blocks, slidelevel):
    section = []
    for b in blocks:
        if slidelevel == 1 and b == {"t": "HorizontalRule"}:
            if section:
                yield section
            section = []
        elif b["t"] == "Header" and b["c"][0] == 1:
            if section:
                yield section
            section = [b]
        else:
            section += [b]
    if section:
        yield section


# do not cache --> checkforbibdifferences
async def md2htmlblocks(content, cwd):
    """ convert markdown to html using pandoc markdown

    Args:
        content: the markdown string to convert

    Returns:
        html: str: the resulting html

    """
    options = ("--to", "html5")
    # slides detected if file starts with
    # <!-- revealjs --> or <!-- revealjs:S -->
    # where S sets the slidelevel
    if content.startswith("<!-- revealjs"):
        # TODO: Do this without copying content 3 times
        if content[13:].startswith(":") and content[15:].startswith(" -->\n"):
            slidelevel = content[14]
            content = content[20:]
        else:
            slidelevel = "2"
            content = content[18:]
        options = ("--to", "revealjs") + ("--slide-level", slidelevel)

    jsonout = await EVENT_LOOP.create_task(md2json(content, cwd))

    # blocks are grouped into slidesections
    if "revealjs" in options:
        blocks = groupsections(jsonout['blocks'], int(slidelevel))
    else:
        blocks = ([j] for j in jsonout['blocks'])

    global BIBQUEUE
    BIBQUEUE = *(await uniqueciteprocdict(jsonout, cwd)), cwd
    bibid = BIBQUEUE[1]
    EVENT_LOOP.create_task(citeproc())

    # []
    titleblock = await json2titleblock(
        json.dumps({
            "blocks": [],
            "meta": {k: jsonout['meta'][k]
                     for k in {"title",
                               "subtitle",
                               "author",
                               "date"} & jsonout['meta'].keys()},
            "pandoc-api-version": jsonout['pandoc-api-version']}),
        options)

    jsonlist = (
        json.dumps({"blocks": j,
                    "meta": {},
                    "pandoc-api-version": jsonout['pandoc-api-version']})
        for j in blocks)

    htmlblocks = await asyncio.gather(*(
        json2htmlblock(j, cwd, options)
        for j in jsonlist))

    try:
        supbib = jsonout['meta']['suppress-bibliography']['c'] is True
    except KeyError:
        supbib = False

    try:
        refsectit = jsonout['meta']['reference-section-title']['c'][0]['c']
    except (IndexError, KeyError):
        refsectit = ''

    try:
        toc = jsonout['meta']['toc']['c'] is True
    except KeyError:
        toc = False

    try:
        toctitle = jsonout['meta']['toc-title']['c'][0]['c']
    except (IndexError, KeyError):
        toctitle = None

    return (titleblock + htmlblocks,
            supbib,
            refsectit,
            bibid,
            toc,
            toctitle)
