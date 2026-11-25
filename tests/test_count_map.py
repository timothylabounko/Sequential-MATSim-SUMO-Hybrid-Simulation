from matsim_sumo.network import count_map

NET = """<net>
    <location netOffset="0.00,0.00"/>
    <edge id=":j_0" function="internal"><lane id=":j_0_0" shape="0,0 1,1"/></edge>
    <edge id="a" from="x" to="y"><lane id="a_0" shape="0,0 100,0"/></edge>
    <edge id="b" from="y" to="z" shape="100,0 100,100"><lane id="b_0" shape="101,0 101,100"/></edge>
</net>
"""

GEH = """zsid,name,street,directions,edges,count,simulated_1,simulated_2,simulated,geh
Z1,One,One,1,a,400,200,200,200,11.55
Z2,Two,Two,1,b,100,150,150,150,4.47
Z3,Three,Three,1,missing,100,100,100,100,0
"""


def test_edge_shapes_skip_internal_edges_and_prefer_the_edge_shape(tmp_path):
    net = tmp_path / "net.xml"
    net.write_text(NET)
    shapes = count_map.edge_shapes(net)
    assert set(shapes) == {"a", "b"}
    assert shapes["b"][0] == (100.0, 0.0)


def test_values_are_negative_where_the_simulation_is_lower(tmp_path):
    geh_csv = tmp_path / "geh.csv"
    geh_csv.write_text(GEH)
    one, two, _ = count_map.read_sections(geh_csv)
    assert count_map.value(one, "percent") == -50.0
    assert count_map.value(two, "difference") == 50.0
    assert count_map.value(one, "geh") < 0 < count_map.value(two, "geh")


def test_map_reports_sections_that_are_not_in_the_network(tmp_path):
    (tmp_path / "net.xml").write_text(NET)
    (tmp_path / "geh.csv").write_text(GEH)
    result = count_map.draw(
        count_map.read_sections(tmp_path / "geh.csv"), count_map.edge_shapes(tmp_path / "net.xml"),
        tmp_path / "map", "percent", "test",
    )
    assert result["sections"] == 2 and result["not_in_network"] == 1
    assert result["fewer_than_counted"] == 1 and result["more_than_counted"] == 1
    assert (tmp_path / "map.png").is_file() and (tmp_path / "map.pdf").is_file()
