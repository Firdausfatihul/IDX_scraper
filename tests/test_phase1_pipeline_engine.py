from __future__ import annotations
import threading, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import idx_digest.pipeline as pipeline_module
from idx_digest.config import Settings
from idx_digest.pipeline import Pipeline, PreparedAttachment

def _item(ticker: str, idx: int) -> dict[str, Any]:
    return {"pengumuman":{"Id2":f"20260807120{idx:02d}-{ticker}","Kode_Emiten":ticker,"TglPengumuman":f"2026-08-07T12:{idx:02d}:00","JudulPengumuman":"Informasi material","NoPengumuman":str(idx),"JenisPengumuman":"General","PerihalPengumuman":"General"},"attachments":[{"FullSavePath":f"https://example.test/{ticker}-{idx}.pdf","OriginalFilename":f"{ticker}-{idx}.pdf","IsAttachment":False}]}

class FakeIDXClient:
    metadata_complete=False
    def __init__(self,*_a,**_k): self.browser=None
    def iter_announcements(self,*_a,**_k):
        for i,t in enumerate(("AAAA","BBBB","CCCC"),start=1): yield _item(t,i)
        FakeIDXClient.metadata_complete=True
    def browser_transport(self): raise AssertionError
    def close(self): pass
class FakeDownloader:
    def __init__(self,*_a,**_k): pass
    def close(self): pass
class FakeSummarizer:
    model="fake-model"; document_prompt_version="doc-v1"; announcement_prompt_version="ann-v1"; company_prompt_version="company-v1"
    def __init__(self):
        self.prompts=SimpleNamespace(profile_name="fake",hashes={}); self.lock=threading.Lock(); self.active=0; self.max_active=0; self.active_tickers=set(); self.overlapped_tickers=False
    @staticmethod
    def is_valid_document_summary(v): return bool(v and v.get("summary"))
    @staticmethod
    def is_valid_announcement_summary(v): return bool(v and v.get("executive_summary"))
    def _enter(self,t):
        with self.lock:
            self.active+=1; self.max_active=max(self.max_active,self.active); self.active_tickers.add(t); self.overlapped_tickers |= len(self.active_tickers)>1
    def _leave(self,t):
        with self.lock: self.active-=1; self.active_tickers.discard(t)
    def summarize_document(self,*,ticker,filename,text,**_k):
        self._enter(ticker); time.sleep(.06); self._leave(ticker); return {"ticker":ticker,"summary":f"{filename}:{text}","chunk_count":1}
    def summarize_announcement(self,*,announcement,documents,**_k):
        t=str(announcement["ticker"]); self._enter(t); time.sleep(.03); self._leave(t); return {"ticker":t,"announcement_id":str(announcement["id2"]),"announced_at":str(announcement["announced_at"]),"title":str(announcement["title"]),"executive_summary":f"{t} announcement"}
    def summarize_company_window(self,*,ticker,**_k):
        self._enter(ticker); time.sleep(.03); self._leave(ticker); return {"ticker":ticker,"overview":f"{ticker} company"}
    def close(self): pass

def test_phase1_prefetches_metadata_then_uses_global_llm_pool(tmp_path: Path, monkeypatch):
    FakeIDXClient.metadata_complete=False
    monkeypatch.setattr(pipeline_module,"IDXClient",FakeIDXClient); monkeypatch.setattr(pipeline_module,"AttachmentDownloader",FakeDownloader)
    settings=Settings(data_dir=tmp_path/"data",openrouter_api_key="unused",llm_concurrency=2,llm_per_announcement_concurrency=1,idx_request_delay_seconds=0)
    pipeline=Pipeline(settings,skip_llm=True); fake=FakeSummarizer(); pipeline.summarizer=fake
    def prepare(_d,*,announcement_id,ticker,attachment):
        assert FakeIDXClient.metadata_complete
        path=tmp_path/f"{ticker}-{announcement_id}.txt"; path.write_text(f"text {ticker}")
        return PreparedAttachment(url=str(attachment["FullSavePath"]),filename=str(attachment["OriginalFilename"]),text_path=path)
    monkeypatch.setattr(pipeline,"_download_or_cached_attachment",prepare); monkeypatch.setattr(pipeline,"_refresh_share_exports",lambda *_a,**_k:{}); monkeypatch.setattr(pipeline,"_export_company_checkpoint",lambda *_a,**_k:None)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz=ZoneInfo("Asia/Jakarta")
    report=pipeline.run(start_at=datetime(2026,8,7,0,0,tzinfo=tz),end_at=datetime(2026,8,7,23,59,59,tzinfo=tz))
    assert report["metadata_announcements_collected"]==3; assert report["processed_announcements"]==3; assert fake.max_active==2; assert fake.overlapped_tickers is True; assert report["diagnostics"]["scheduler"]=="global-phase-4"
    assert report["performance"]["processed_announcements"] == 3
    assert "recommendation" in report["performance"]

def test_company_isolation_rejects_foreign_ticker():
    try: Pipeline._assert_company_isolation("ANTM",[{"summary":{"ticker":"ANTM"}},{"summary":{"ticker":"BBCA"}}])
    except ValueError as exc: assert "BBCA" in str(exc)
    else: raise AssertionError("foreign ticker was not rejected")
