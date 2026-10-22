import pytest

from wahoo_lsl.dashboard.graph import Graph, nice_ceiling, segments


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_segments_break_on_none_and_gaps():
    pts = [(0, 1), (1, 2), (2, None), (3, 4), (4, 5), (10, 6), (11, 7)]
    assert segments(pts) == [[(0, 1.0), (1, 2.0)], [(3, 4.0), (4, 5.0)], [(10, 6.0), (11, 7.0)]]


def test_segments_edge_cases():
    assert segments([]) == []
    assert segments([(0, None)]) == []
    assert segments([(0, 3)]) == [[(0, 3.0)]]
    assert segments([(0, 1), (5, 2)]) == [[(0, 1.0), (5, 2.0)]]  # a gap of exactly 5 s is not a break


@pytest.mark.parametrize(
    "value, floor, expected",
    [(0, 100, 100.0), (101, 100, 200.0), (200, 100, 200.0), (201, 100, 500.0),
     (999, 10, 1000.0), (1000, 10, 1000.0), (7.3, 10, 10.0), (0, 0, 1.0), (0.3, 0, 0.5)],
)
def test_nice_ceiling(value, floor, expected):
    assert nice_ceiling(value, floor) == pytest.approx(expected)


def test_graph_draws_one_item_per_run_and_redraws_cleanly(tk_root):
    g = Graph(tk_root, "Power", "W", 100)
    g.canvas.pack()
    tk_root.update()
    g.draw([(0, 100), (1, 110), (2, None), (3, 120), (20, 90)])
    assert len(g.canvas.find_withtag("data")) == 3  # one line and two isolated points
    texts = [g.canvas.itemcget(i, "text") for i in g.canvas.find_all() if g.canvas.type(i) == "text"]
    assert "200" in texts  # y axis max = nice_ceiling(120, 100)
    g.draw([])
    assert g.canvas.find_withtag("data") == ()
