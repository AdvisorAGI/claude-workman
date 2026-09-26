import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from permission_hint import placement

def test_hint_never_covers_target_or_leaves_screen():
    for x,y in [(0,0),(100,100),(1190,600),(600,330)]:
        target=(x,y,80,40);rect=placement(target,(0,0,1280,720))
        assert rect is not None
        a,b,w,h=rect
        assert 0<=a<=1280-w and 0<=b<=720-h
        assert a+w<=x or a>=x+80 or b+h<=y or b>=y+40

def test_no_room_uses_plain_text_fallback():
    assert placement((0,0,300,200),(0,0,300,200)) is None
