# Copyright (c) 2001, 2002, 2003, 2004, 2005, 2006 Python Software Foundation.
# All rights reserved.

# mypy: allow-untyped-defs, allow-untyped-calls

"""Tokenization help for Python programs.

generate_tokens(readline) is a generator that breaks a stream of
text into Python tokens.  It accepts a readline-like method which is called
repeatedly to get the next line of input (or "" for EOF).  It generates
5-tuples with these members:

    the token type (see token.py)
    the token (a string)
    the starting (row, column) indices of the token (a 2-tuple of ints)
    the ending (row, column) indices of the token (a 2-tuple of ints)
    the original line (string)

It is designed to match the working of the Python tokenizer exactly, except
that it produces COMMENT tokens for comments and gives type OP for all
operators

Older entry points
    tokenize_loop(readline, tokeneater)
    tokenize(readline, tokeneater=printtoken)
are the same, except instead of generating tokens, tokeneater is a callback
function to which the 5 fields described above are passed as 5 arguments,
each time a new token is found."""

import sys
from typing import (
    Callable,
    Final,
    Iterable,
    Iterator,
    List,
    Optional,
    Pattern,
    Set,
    Tuple,
    Union,
    cast,
)

from blib2to3.pgen2.grammar import Grammar
from blib2to3.pgen2.token import (
    ASYNC,
    AWAIT,
    COMMENT,
    DEDENT,
    ENDMARKER,
    ERRORTOKEN,
    FSTRING_END,
    FSTRING_MIDDLE,
    FSTRING_START,
    INDENT,
    LBRACE,
    NAME,
    NEWLINE,
    NL,
    NUMBER,
    OP,
    RBRACE,
    STRING,
    tok_name,
)

__author__ = "Ka-Ping Yee <ping@lfw.org>"
__credits__ = "GvR, ESR, Tim Peters, Thomas Wouters, Fred Drake, Skip Montanaro"

import re
from codecs import BOM_UTF8, lookup

from . import token

__all__ = [x for x in dir(token) if x[0] != "_"] + [
    "tokenize",
    "generate_tokens",
    "untokenize",
]
del token


def group(*choices: str) -> str:
    return "(" + "|".join(choices) + ")"


def any(*choices: str) -> str:
    return group(*choices) + "*"


def maybe(*choices: str) -> str:
    return group(*choices) + "?"


def _combinations(*l: str) -> Set[str]:
    return {x + y for x in l for y in l + ("",) if x.casefold() != y.casefold()}


Whitespace = r"[ \f\t]*"
Comment = r"#[^\r\n]*"
Ignore = Whitespace + any(r"\\\r?\n" + Whitespace) + maybe(Comment)
Name = (  # this is invalid but it's fine because Name comes after Number in all groups
    r"[^\s#\(\)\[\]\{\}+\-*/!@$%^&=|;:'\",\.<>/?`~\\]+"
)

Binnumber = r"0[bB]_?[01]+(?:_[01]+)*"
Hexnumber = r"0[xX]_?[\da-fA-F]+(?:_[\da-fA-F]+)*[lL]?"
Octnumber = r"0[oO]?_?[0-7]+(?:_[0-7]+)*[lL]?"
Decnumber = group(r"[1-9]\d*(?:_\d+)*[lL]?", "0[lL]?")
Intnumber = group(Binnumber, Hexnumber, Octnumber, Decnumber)
Exponent = r"[eE][-+]?\d+(?:_\d+)*"
Pointfloat = group(r"\d+(?:_\d+)*\.(?:\d+(?:_\d+)*)?", r"\.\d+(?:_\d+)*") + maybe(
    Exponent
)
Expfloat = r"\d+(?:_\d+)*" + Exponent
Floatnumber = group(Pointfloat, Expfloat)
Imagnumber = group(r"\d+(?:_\d+)*[jJ]", Floatnumber + r"[jJ]")
Number = group(Imagnumber, Floatnumber, Intnumber)

# Tail end of ' string.
Single = r"[^'\\]*(?:\\.[^'\\]*)*'"
# Tail end of " string.
Double = r'[^"\\]*(?:\\.[^"\\]*)*"'
# Tail end of ''' string.
Single3 = r"[^'\\]*(?:(?:\\.|'(?!''))[^'\\]*)*'''"
# Tail end of """ string.
Double3 = r'[^"\\]*(?:(?:\\.|"(?!""))[^"\\]*)*"""'
_litprefix = r"(?:[uUrRbB]|[rR][bB]|[bBuU][rR])?"
_fstringlitprefix = r"(?:rF|FR|Fr|fr|RF|F|rf|f|Rf|fR)"
Triple = group(
    _litprefix + "'''",
    _litprefix + '"""',
    _fstringlitprefix + '"""',
    _fstringlitprefix + "'''",
)

SingleLbrace = r"[^'\\{]*(?:(?:\\.|{{)[^'\\{]*)*{(?!{)"
DoubleLbrace = r'[^"\\{]*(?:(?:\\.|{{)[^"\\{]*)*{(?!{)'

Single3Lbrace = r"[^'\\{]*(?:(?:\\.|{{|'(?!''))[^'\\{]*)*{(?!{)"
Double3Lbrace = r'[^"\\{]*(?:(?:\\.|{{|"(?!""))[^"\\{]*)*{(?!{)'

# Because of leftmost-then-longest match semantics, be sure to put the
# longest operators first (e.g., if = came before ==, == would get
# recognized as two instances of =).
Operator = group(
    r"\*\*=?",
    r">>=?",
    r"<<=?",
    r"<>",
    r"!=",
    r"//=?",
    r"->",
    r"[+\-*/%&@|^=<>:]=?",
    r"~",
)

Bracket = "[][(){}]"
Special = group(r"\r?\n", r"[:;.,`@]")
Funny = group(Operator, Bracket, Special)

_string_middle_single = r"[^\n'\\]*(?:\\.[^\n'\\]*)*"
_string_middle_double = r'[^\n"\\]*(?:\\.[^\n"\\]*)*'

# FSTRING_MIDDLE and LBRACE, inside a single quoted fstring
_fstring_middle_single = r"[^\n'\\{]*(?:(?:\\.|{{)[^\n'\\{]*)*({)(?!{)"
_fstring_middle_double = r'[^\n"\\{]*(?:(?:\\.|{{)[^\n"\\{]*)*({)(?!{)'

# First (or only) line of ' or " string.
ContStr = group(
    _litprefix + "'" + _string_middle_single + group("'", r"\\\r?\n"),
    _litprefix + '"' + _string_middle_double + group('"', r"\\\r?\n"),
    group(_fstringlitprefix + "'") + _fstring_middle_single,
    group(_fstringlitprefix + '"') + _fstring_middle_double,
    group(_fstringlitprefix + "'") + _string_middle_single + group("'", r"\\\r?\n"),
    group(_fstringlitprefix + '"') + _string_middle_double + group('"', r"\\\r?\n"),
)
PseudoExtras = group(r"\\\r?\n", Comment, Triple)
PseudoToken = Whitespace + group(PseudoExtras, Number, Funny, ContStr, Name)

pseudoprog: Final = re.compile(PseudoToken, re.UNICODE)

singleprog = re.compile(Single)
singleprog_plus_lbrace = re.compile(group(SingleLbrace, Single))
doubleprog = re.compile(Double)
doubleprog_plus_lbrace = re.compile(group(DoubleLbrace, Double))

single3prog = re.compile(Single3)
single3prog_plus_lbrace = re.compile(group(Single3Lbrace, Single3))
double3prog = re.compile(Double3)
double3prog_plus_lbrace = re.compile(group(Double3Lbrace, Double3))

_strprefixes = _combinations("r", "R", "b", "B") | {"u", "U", "ur", "uR", "Ur", "UR"}
_fstring_prefixes = _combinations("r", "R", "f", "F") - {"r", "R"}

endprogs: Final = {
    "'": singleprog,
    '"': doubleprog,
    "'''": single3prog,
    '"""': double3prog,
    **{f"{prefix}'": singleprog for prefix in _strprefixes},
    **{f'{prefix}"': doubleprog for prefix in _strprefixes},
    **{f"{prefix}'": singleprog_plus_lbrace for prefix in _fstring_prefixes},
    **{f'{prefix}"': doubleprog_plus_lbrace for prefix in _fstring_prefixes},
    **{f"{prefix}'''": single3prog for prefix in _strprefixes},
    **{f'{prefix}"""': double3prog for prefix in _strprefixes},
    **{f"{prefix}'''": single3prog_plus_lbrace for prefix in _fstring_prefixes},
    **{f'{prefix}"""': double3prog_plus_lbrace for prefix in _fstring_prefixes},
}

triple_quoted: Final = (
    {"'''", '"""'}
    | {f"{prefix}'''" for prefix in _strprefixes | _fstring_prefixes}
    | {f'{prefix}"""' for prefix in _strprefixes | _fstring_prefixes}
)
single_quoted: Final = (
    {"'", '"'}
    | {f"{prefix}'" for prefix in _strprefixes | _fstring_prefixes}
    | {f'{prefix}"' for prefix in _strprefixes | _fstring_prefixes}
)

tabsize = 8


class TokenError(Exception):
    pass


class StopTokenizing(Exception):
    pass


Coord = Tuple[int, int]


def printtoken(
    type: int, token: str, srow_col: Coord, erow_col: Coord, line: str
) -> None:  # for testing
    (srow, scol) = srow_col
    (erow, ecol) = erow_col
    print(
        "%d,%d-%d,%d:\t%s\t%s" % (srow, scol, erow, ecol, tok_name[type], repr(token))
    )


TokenEater = Callable[[int, str, Coord, Coord, str], None]


def tokenize(readline: Callable[[], str], tokeneater: TokenEater = printtoken) -> None:
    """
    The tokenize() function accepts two parameters: one representing the
    input stream, and one providing an output mechanism for tokenize().

    The first parameter, readline, must be a callable object which provides
    the same interface as the readline() method of built-in file objects.
    Each call to the function should return one line of input as a string.

    The second parameter, tokeneater, must also be a callable object. It is
    called once for each token, with five arguments, corresponding to the
    tuples generated by generate_tokens().
    """
    try:
        tokenize_loop(readline, tokeneater)
    except StopTokenizing:
        pass


# backwards compatible interface
def tokenize_loop(readline: Callable[[], str], tokeneater: TokenEater) -> None:
    for token_info in generate_tokens(readline):
        tokeneater(*token_info)


GoodTokenInfo = Tuple[int, str, Coord, Coord, str]
TokenInfo = Union[Tuple[int, str], GoodTokenInfo]


class Untokenizer:
    tokens: List[str]
    prev_row: int
    prev_col: int

    def __init__(self) -> None:
        self.tokens = []
        self.prev_row = 1
        self.prev_col = 0

    def add_whitespace(self, start: Coord) -> None:
        row, col = start
        assert row <= self.prev_row
        col_offset = col - self.prev_col
        if col_offset:
            self.tokens.append(" " * col_offset)

    def untokenize(self, iterable: Iterable[TokenInfo]) -> str:
        for t in iterable:
            if len(t) == 2:
                self.compat(cast(Tuple[int, str], t), iterable)
                break
            tok_type, token, start, end, line = cast(
                Tuple[int, str, Coord, Coord, str], t
            )
            self.add_whitespace(start)
            self.tokens.append(token)
            self.prev_row, self.prev_col = end
            if tok_type in (NEWLINE, NL):
                self.prev_row += 1
                self.prev_col = 0
        return "".join(self.tokens)

    def compat(self, token: Tuple[int, str], iterable: Iterable[TokenInfo]) -> None:
        startline = False
        indents = []
        toks_append = self.tokens.append
        toknum, tokval = token
        if toknum in (NAME, NUMBER):
            tokval += " "
        if toknum in (NEWLINE, NL):
            startline = True
        for tok in iterable:
            toknum, tokval = tok[:2]

            if toknum in (NAME, NUMBER, ASYNC, AWAIT):
                tokval += " "

            if toknum == INDENT:
                indents.append(tokval)
                continue
            elif toknum == DEDENT:
                indents.pop()
                continue
            elif toknum in (NEWLINE, NL):
                startline = True
            elif startline and indents:
                toks_append(indents[-1])
                startline = False
            toks_append(tokval)


cookie_re = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", re.ASCII)
blank_re = re.compile(rb"^[ \t\f]*(?:[#\r\n]|$)", re.ASCII)


def _get_normal_name(orig_enc: str) -> str:
    """Imitates get_normal_name in tokenizer.c."""
    # Only care about the first 12 characters.
    enc = orig_enc[:12].lower().replace("_", "-")
    if enc == "utf-8" or enc.startswith("utf-8-"):
        return "utf-8"
    if enc in ("latin-1", "iso-8859-1", "iso-latin-1") or enc.startswith(
        ("latin-1-", "iso-8859-1-", "iso-latin-1-")
    ):
        return "iso-8859-1"
    return orig_enc


def detect_encoding(readline: Callable[[], bytes]) -> Tuple[str, List[bytes]]:
    """
    The detect_encoding() function is used to detect the encoding that should
    be used to decode a Python source file. It requires one argument, readline,
    in the same way as the tokenize() generator.

    It will call readline a maximum of twice, and return the encoding used
    (as a string) and a list of any lines (left as bytes) it has read
    in.

    It detects the encoding from the presence of a utf-8 bom or an encoding
    cookie as specified in pep-0263. If both a bom and a cookie are present, but
    disagree, a SyntaxError will be raised. If the encoding cookie is an invalid
    charset, raise a SyntaxError.  Note that if a utf-8 bom is found,
    'utf-8-sig' is returned.

    If no encoding is specified, then the default of 'utf-8' will be returned.
    """
    bom_found = False
    encoding = None
    default = "utf-8"

    def read_or_stop() -> bytes:
        try:
            return readline()
        except StopIteration:
            return b""

    def find_cookie(line: bytes) -> Optional[str]:
        try:
            line_string = line.decode("ascii")
        except UnicodeDecodeError:
            return None
        match = cookie_re.match(line_string)
        if not match:
            return None
        encoding = _get_normal_name(match.group(1))
        try:
            codec = lookup(encoding)
        except LookupError:
            # This behaviour mimics the Python interpreter
            raise SyntaxError("unknown encoding: " + encoding)

        if bom_found:
            if codec.name != "utf-8":
                # This behaviour mimics the Python interpreter
                raise SyntaxError("encoding problem: utf-8")
            encoding += "-sig"
        return encoding

    first = read_or_stop()
    if first.startswith(BOM_UTF8):
        bom_found = True
        first = first[3:]
        default = "utf-8-sig"
    if not first:
        return default, []

    encoding = find_cookie(first)
    if encoding:
        return encoding, [first]
    if not blank_re.match(first):
        return default, [first]

    second = read_or_stop()
    if not second:
        return default, [first]

    encoding = find_cookie(second)
    if encoding:
        return encoding, [first, second]

    return default, [first, second]


def untokenize(iterable: Iterable[TokenInfo]) -> str:
    """Transform tokens back into Python source code.

    Each element returned by the iterable must be a token sequence
    with at least two elements, a token number and token value.  If
    only two tokens are passed, the resulting output is poor.

    Round-trip invariant for full input:
        Untokenized source will match input source exactly

    Round-trip invariant for limited input:
        # Output text will tokenize the back to the input
        t1 = [tok[:2] for tok in generate_tokens(f.readline)]
        newcode = untokenize(t1)
        readline = iter(newcode.splitlines(1)).next
        t2 = [tok[:2] for tokin generate_tokens(readline)]
        assert t1 == t2
    """
    ut = Untokenizer()
    return ut.untokenize(iterable)


def generate_tokens(
    readline: Callable[[], str], grammar: Optional[Grammar] = None
) -> Iterator[GoodTokenInfo]:
    """
    The generate_tokens() generator requires one argument, readline, which
    must be a callable object which provides the same interface as the
    readline() method of built-in file objects. Each call to the function
    should return one line of input as a string.  Alternately, readline
    can be a callable function terminating with StopIteration:
        readline = open(myfile).next    # Example of alternate readline

    The generator produces 5-tuples with these members: the token type; the
    token string; a 2-tuple (srow, scol) of ints specifying the row and
    column where the token begins in the source; a 2-tuple (erow, ecol) of
    ints specifying the row and column where the token ends in the source;
    and the line on which the token was found. The line passed is the
    logical line; continuation lines are included.
    """
    lnum = parenlev = fstring_level = continued = 0
    inside_fstring_braces = False
    numchars: Final[str] = "0123456789"
    contstr, needcont = "", 0
    contline: Optional[str] = None
    indents = [0]

    # If we know we're parsing 3.7+, we can unconditionally parse `async` and
    # `await` as keywords.
    async_keywords = False if grammar is None else grammar.async_keywords
    # 'stashed' and 'async_*' are used for async/await parsing
    stashed: Optional[GoodTokenInfo] = None
    async_def = False
    async_def_indent = 0
    async_def_nl = False

    strstart: Tuple[int, int]
    endprog_stack: list[Pattern[str]] = []

    while 1:  # loop over lines in stream
        try:
            line = readline()
        except StopIteration:
            line = ""
        lnum += 1
        pos, max = 0, len(line)

        # TODO: probably inside_fstring_braces is not the best boolean.
        # what about a case of a string inside a multiline fstring inside a
        # multiline fstring??
        # for eg. this doesn't work right now: f"{f'{2+2}'}"
        # because inside_fstring_braces gets set to false after the first `}`
        # print(f'{parenlev = } {continued = } {inside_fstring_braces = }')
        if contstr and not inside_fstring_braces:  # continued string
            assert contline is not None
            if not line:
                raise TokenError("EOF in multi-line string", strstart)
            endprog = endprog_stack[-1]
            endmatch = endprog.match(line)
            if endmatch:
                pos = end = endmatch.end(0)
                token = contstr + line[:end]
                spos = strstart
                epos = (lnum, end)
                tokenline = contline + line
                # TODO: better way to detect fstring
                if fstring_level == 0:
                    yield (STRING, token, spos, epos, tokenline)
                else:
                    # TODO: positions are all wrong
                    yield (FSTRING_MIDDLE, token, spos, epos, tokenline)
                    if token.endswith("{"):
                        yield (LBRACE, "{", spos, epos, tokenline)
                        inside_fstring_braces = True
                    else:
                        yield (FSTRING_END, token[-1], spos, epos, tokenline)
                        fstring_level -= 1
                        endprog_stack.pop()
                    # TODO: contstr reliance doesn't work now because we can be inside
                    # an fstring and still empty contstr right here.
                contstr, needcont = "", 0
                contline = None
            elif needcont and line[-2:] != "\\\n" and line[-3:] != "\\\r\n":
                yield (
                    ERRORTOKEN,
                    contstr + line,
                    strstart,
                    (lnum, len(line)),
                    contline,
                )
                contstr = ""
                contline = None
                continue
            else:
                contstr = contstr + line
                contline = contline + line
                continue

        # new statement
        elif parenlev == 0 and not continued and not inside_fstring_braces:
            if not line:
                break
            column = 0
            while pos < max:  # measure leading whitespace
                if line[pos] == " ":
                    column += 1
                elif line[pos] == "\t":
                    column = (column // tabsize + 1) * tabsize
                elif line[pos] == "\f":
                    column = 0
                else:
                    break
                pos += 1
            if pos == max:
                break

            if stashed:
                yield stashed
                stashed = None

            if line[pos] in "\r\n":  # skip blank lines
                yield (NL, line[pos:], (lnum, pos), (lnum, len(line)), line)
                continue

            if line[pos] == "#":  # skip comments
                comment_token = line[pos:].rstrip("\r\n")
                nl_pos = pos + len(comment_token)
                yield (
                    COMMENT,
                    comment_token,
                    (lnum, pos),
                    (lnum, nl_pos),
                    line,
                )
                yield (NL, line[nl_pos:], (lnum, nl_pos), (lnum, len(line)), line)
                continue

            if column > indents[-1]:  # count indents
                indents.append(column)
                yield (INDENT, line[:pos], (lnum, 0), (lnum, pos), line)

            while column < indents[-1]:  # count dedents
                if column not in indents:
                    raise IndentationError(
                        "unindent does not match any outer indentation level",
                        ("<tokenize>", lnum, pos, line),
                    )
                indents = indents[:-1]

                if async_def and async_def_indent >= indents[-1]:
                    async_def = False
                    async_def_nl = False
                    async_def_indent = 0

                yield (DEDENT, "", (lnum, pos), (lnum, pos), line)

            if async_def and async_def_nl and async_def_indent >= indents[-1]:
                async_def = False
                async_def_nl = False
                async_def_indent = 0

        else:  # continued statement
            if not line:
                raise TokenError("EOF in multi-line statement", (lnum, 0))
            continued = 0

        while pos < max:
            if fstring_level > 0 and not inside_fstring_braces:
                endprog = endprog_stack[-1]
                endmatch = endprog.match(line, pos)
                if endmatch:  # all on one line
                    start, end = endmatch.span(0)
                    token = line[start:end]
                    # TODO: triple quotes
                    # TODO: check if the token will ever have any whitespace around?
                    middle_token, end_token = token[:-1], token[-1]
                    # TODO: unsure if this can be safely removed
                    if stashed:
                        yield stashed
                        stashed = None
                    yield (
                        FSTRING_MIDDLE,
                        middle_token,
                        (lnum, pos),
                        (lnum, end - 1),
                        line,
                    )
                    if not token.endswith("{"):
                        # TODO: end-1 is probably wrong
                        yield (
                            FSTRING_END,
                            end_token,
                            (lnum, end - 1),
                            (lnum, end),
                            line,
                        )
                        fstring_level -= 1
                        endprog_stack.pop()
                    else:
                        # TODO: most of the positions are wrong
                        yield (LBRACE, "{", (lnum, 0), (lnum, 0), line)
                        inside_fstring_braces = True
                    pos = end
                else:  # multiple lines
                    contstr += line
                    contline = line
                    break

            pseudomatch = pseudoprog.match(line, pos)
            if pseudomatch:  # scan for tokens
                start, end = pseudomatch.span(1)
                spos, epos, pos = (lnum, start), (lnum, end), end
                token, initial = line[start:end], line[start]

                if initial in numchars or (
                    initial == "." and token != "."
                ):  # ordinary number
                    yield (NUMBER, token, spos, epos, line)
                elif initial in "\r\n":
                    newline = NEWLINE
                    if parenlev > 0 or inside_fstring_braces:
                        newline = NL
                    elif async_def:
                        async_def_nl = True
                    if stashed:
                        yield stashed
                        stashed = None
                    yield (newline, token, spos, epos, line)

                elif initial == "#":
                    assert not token.endswith("\n")
                    if stashed:
                        yield stashed
                        stashed = None
                    yield (COMMENT, token, spos, epos, line)
                elif token in triple_quoted:
                    endprog = endprogs[token]
                    if token.startswith("f"):
                        yield (FSTRING_START, token, spos, epos, line)
                        fstring_level += 1
                        endprog_stack.append(endprog)

                    endmatch = endprog.match(line, pos)
                    if endmatch:  # all on one line
                        if stashed:
                            yield stashed
                            stashed = None
                        # TODO: move this logic to a function
                        # TODO: not how you should identify FSTRING_START
                        if not token.startswith("f"):
                            pos = endmatch.end(0)
                            token = line[start:pos]
                            yield (STRING, token, spos, epos, line)
                        else:
                            end = endmatch.end(0)
                            token = line[pos:end]
                            spos, epos = (lnum, pos), (lnum, end)
                            # TODO: confirm there will be no padding around the tokens
                            # TODO: don't detect like this perhaps?
                            if not token.endswith("{"):
                                fstring_middle, fstring_end = token[:-3], token[-3:]
                                fstring_middle_epos = fstring_end_spos = (lnum, end - 3)
                                yield (
                                    FSTRING_MIDDLE,
                                    fstring_middle,
                                    spos,
                                    fstring_middle_epos,
                                    line,
                                )
                                yield (
                                    FSTRING_END,
                                    fstring_end,
                                    fstring_end_spos,
                                    epos,
                                    line,
                                )
                                fstring_level -= 1
                                endprog_stack.pop()
                            else:
                                fstring_middle, lbrace = token[:-1], token[-1]
                                fstring_middle_epos = lbrace_spos = (lnum, end - 1)
                                yield (
                                    FSTRING_MIDDLE,
                                    fstring_middle,
                                    spos,
                                    fstring_middle_epos,
                                    line,
                                )
                                yield (LBRACE, lbrace, lbrace_spos, epos, line)
                                inside_fstring_braces = True
                            pos = end
                    else:
                        strstart = (lnum, start)  # multiple lines
                        contstr = line[start:]
                        contline = line
                        break
                elif (
                    initial in single_quoted
                    or token[:2] in single_quoted
                    or token[:3] in single_quoted
                ):
                    maybe_endprog = (
                        endprogs.get(initial)
                        or endprogs.get(token[:2])
                        or endprogs.get(token[:3])
                    )
                    assert maybe_endprog is not None, f"endprog not found for {token}"
                    endprog = maybe_endprog
                    if token[-1] == "\n":  # continued string
                        endprog_stack.append(endprog)
                        strstart = (lnum, start)
                        contstr, needcont = line[start:], 1
                        contline = line
                        break
                    else:  # ordinary string
                        if stashed:
                            yield stashed
                            stashed = None

                        # TODO: move this logic to a function
                        if not token.startswith("f"):
                            yield (STRING, token, spos, epos, line)
                        else:
                            if pseudomatch[20] is not None:
                                fstring_start = pseudomatch[20]
                                offset = pseudomatch.end(20) - pseudomatch.start()
                                start_epos = (lnum, start + offset)
                            elif pseudomatch[22] is not None:
                                fstring_start = pseudomatch[22]
                                offset = pseudomatch.end(22) - pseudomatch.start()
                                start_epos = (lnum, start + offset)
                            elif pseudomatch[24] is not None:
                                fstring_start = pseudomatch[24]
                                offset = pseudomatch.end(24) - pseudomatch.start()
                                start_epos = (lnum, start + offset)
                            else:
                                fstring_start = pseudomatch[26]
                                offset = pseudomatch.end(26) - pseudomatch.start()
                                start_epos = (lnum, start + offset)
                            yield (FSTRING_START, fstring_start, spos, start_epos, line)
                            fstring_level += 1
                            endprog = endprogs[fstring_start]
                            endprog_stack.append(endprog)

                            end_offset = pseudomatch.end() - 1
                            fstring_middle = line[start + offset : end_offset]
                            middle_spos = (lnum, start + offset)
                            middle_epos = (lnum, end_offset)
                            yield (
                                FSTRING_MIDDLE,
                                fstring_middle,
                                middle_spos,
                                middle_epos,
                                line,
                            )
                            if not token.endswith("{"):
                                end_spos = (lnum, end_offset)
                                end_epos = (lnum, end_offset + 1)
                                yield (FSTRING_END, token[-1], end_spos, end_epos, line)
                                fstring_level -= 1
                                endprog_stack.pop()
                            else:
                                end_spos = (lnum, end_offset)
                                end_epos = (lnum, end_offset + 1)
                                yield (LBRACE, "{", end_spos, end_epos, line)
                                inside_fstring_braces = True

                elif initial.isidentifier():  # ordinary name
                    if token in ("async", "await"):
                        if async_keywords or async_def:
                            yield (
                                ASYNC if token == "async" else AWAIT,
                                token,
                                spos,
                                epos,
                                line,
                            )
                            continue

                    tok = (NAME, token, spos, epos, line)
                    if token == "async" and not stashed:
                        stashed = tok
                        continue

                    if token in ("def", "for"):
                        if stashed and stashed[0] == NAME and stashed[1] == "async":
                            if token == "def":
                                async_def = True
                                async_def_indent = indents[-1]

                            yield (
                                ASYNC,
                                stashed[1],
                                stashed[2],
                                stashed[3],
                                stashed[4],
                            )
                            stashed = None

                    if stashed:
                        yield stashed
                        stashed = None

                    yield tok
                elif initial == "\\":  # continued stmt
                    # This yield is new; needed for better idempotency:
                    if stashed:
                        yield stashed
                        stashed = None
                    yield (NL, token, spos, (lnum, pos), line)
                    continued = 1
                elif initial == "}" and parenlev == 0 and inside_fstring_braces:
                    yield (RBRACE, token, spos, epos, line)
                    inside_fstring_braces = False
                else:
                    if initial in "([{":
                        parenlev += 1
                    elif initial in ")]}":
                        parenlev -= 1
                    if stashed:
                        yield stashed
                        stashed = None
                    yield (OP, token, spos, epos, line)
            else:
                yield (ERRORTOKEN, line[pos], (lnum, pos), (lnum, pos + 1), line)
                pos += 1

    if stashed:
        yield stashed
        stashed = None

    for _indent in indents[1:]:  # pop remaining indent levels
        yield (DEDENT, "", (lnum, 0), (lnum, 0), "")
    yield (ENDMARKER, "", (lnum, 0), (lnum, 0), "")


if __name__ == "__main__":  # testing
    if len(sys.argv) > 1:
        tokenize(open(sys.argv[1]).readline)
    else:
        tokenize(sys.stdin.readline)
