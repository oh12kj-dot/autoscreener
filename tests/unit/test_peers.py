from autoscreener.screening.peers import PeerCandidate, select_peers


def candidate(symbol, industry="Software", sector="Tech", cap=100):
    return PeerCandidate(symbol, industry, sector, cap)


def test_falls_back_to_sector_and_includes_target():
    target = candidate("A")
    result = select_peers(target, [target, candidate("B"), candidate("C", industry="Other"), candidate("D", industry="Other")])
    assert result.peer_basis == "sector"
    assert target in result.peers


def test_returns_none_when_too_few_peers():
    target = candidate("A")
    assert select_peers(target, [target, candidate("B")]).peer_basis == "none"
