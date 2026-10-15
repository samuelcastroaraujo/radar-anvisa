from app.ingest import dados_abertos_alimentos as da

CSV = (
    "NU_CNPJ_EMPRESA;NO_RAZAO_SOCIAL_EMPRESA;NO_PRODUTO;NU_PROCESSO;DS_TIPO_PRODUTO;"
    "DS_CATEGORIA_PRODUTO;DT_FINALIZACAO_PROCESSO;NU_REGISTRO_PRODUTO;"
    "DT_VENCIMENTO_REGISTRO;ST_SITUACAO_REGISTRO\n"
    "07426908000453;TRUST IMPORTAÇÃO LTDA;WHEY PROTEIN ISOLADO;25351157681202675;ALIMENTO;"
    "Suplementos alimentares;05/19/2024 00:00:00;25351157681;12/01/2029 00:00:00;Ativo\n"
    "00000000055417;BISCOLAR LTDA;BISCOITO DOCE;2500499035688;ALIMENTO;"
    "CATEGORIA NAO CADASTRADA;;441280005;02/01/2003 00:00:00;Inativo\n"
).encode("latin-1")


def test_filtra_por_nome_sem_acento_e_situacao() -> None:
    linhas = da.parse_csv(CSV)
    assert len(linhas) == 2
    achados = da.filtrar(linhas, nome_produto="whey", situacao_registro="Ativo")
    assert [ln.processo for ln in achados] == ["25351157681202675"]
    assert achados[0].vencimento is not None and achados[0].vencimento.month == 12


def test_filtra_por_empresa_nome_e_cnpj() -> None:
    linhas = da.parse_csv(CSV)
    assert len(da.filtrar(linhas, detentor_registro="importacao")) == 1
    assert len(da.filtrar(linhas, detentor_registro="07.426.908/0004-53")) == 1
    assert da.filtrar(linhas, detentor_registro="inexistente") == []
