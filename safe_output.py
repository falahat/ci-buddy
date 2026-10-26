"""Display untrusted UTF-8 as text without terminal or bidirectional controls."""
import codecs
import sys


def clean(text):
    return ''.join(char for char in text if char in '\n\t' or char.isprintable())


def stream(source, destination):
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    while block := source.read1(65536):
        destination.write(clean(decoder.decode(block)))
        destination.flush()
    destination.write(clean(decoder.decode(b'', final=True)))
    destination.flush()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    stream(sys.stdin.buffer, sys.stdout)
