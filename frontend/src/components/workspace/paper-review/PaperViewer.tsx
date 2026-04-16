import { useState, useEffect, useCallback } from 'react';
import { apiPost } from '@/api/client';
import { RewriteOverlay } from './RewriteOverlay';
import type { SessionRun } from '@/types';

interface PaperViewerProps {
  run: SessionRun;
  paperVersion: number;
  isRewriting: boolean;
  theoryStatus: string;
  writerStatus: string;
}

interface CompileResponse {
  ok?: boolean;
  error?: string;
  pdf_path?: string;
}

export function PaperViewer({ run, paperVersion, isRewriting, theoryStatus, writerStatus }: PaperViewerProps) {
  const [activeTab, setActiveTab] = useState<'pdf' | 'latex'>('pdf');
  const [compiling, setCompiling] = useState(false);
  const [compileError, setCompileError] = useState('');
  // Whether a PDF is available to display in the iframe.
  // Set true after successful compile or after HEAD-probing the artifact.
  const [pdfAvailable, setPdfAvailable] = useState(false);
  const [prevVersion, setPrevVersion] = useState(paperVersion);

  // Reset PDF state when paper version changes (after rewrite).
  if (paperVersion !== prevVersion) {
    setPrevVersion(paperVersion);
    setPdfAvailable(false);
    setCompileError('');
  }

  // Clear when a rewrite starts so the overlay replaces the iframe.
  const [wasRewriting, setWasRewriting] = useState(isRewriting);
  if (isRewriting && !wasRewriting) {
    setPdfAvailable(false);
    setCompileError('');
  }
  if (isRewriting !== wasRewriting) {
    setWasRewriting(isRewriting);
  }

  // On mount and when version changes, check if PDF exists. If not,
  // auto-compile it so the user sees the PDF immediately.
  const pdfUrl = `/api/runs/${run.run_id}/artifacts/paper.pdf`;
  useEffect(() => {
    if (pdfAvailable || isRewriting || compiling) return;
    let cancelled = false;
    void (async () => {
      try {
        const ctrl = new AbortController();
        const res = await fetch(pdfUrl, { signal: ctrl.signal });
        ctrl.abort();
        if (cancelled) return;
        if (res.ok) {
          setPdfAvailable(true);
        } else {
          // PDF not found — auto-compile if LaTeX is available
          const wt = run.pipeline?.find((t) => t.name === 'writer');
          const hasLatex = !!(
            (wt?.outputs?.latex_paper) || run.result?.latex_paper
          );
          if (hasLatex) {
            setCompiling(true);
            try {
              const compileRes = await apiPost<CompileResponse>(
                `/api/runs/${run.run_id}/compile-pdf`, {}
              );
              if (!cancelled) {
                if (compileRes.ok) {
                  setPdfAvailable(true);
                } else {
                  setCompileError(compileRes.error || 'Compilation failed');
                }
              }
            } catch (e) {
              if (!cancelled) setCompileError(String(e));
            } finally {
              if (!cancelled) setCompiling(false);
            }
          }
        }
      } catch {
        // Network error — ignore
      }
    })();
    return () => { cancelled = true; };
  }, [pdfUrl, paperVersion, isRewriting, pdfAvailable, compiling, run.run_id, run.pipeline, run.result]);

  // Source LaTeX from writer task outputs (available during gate) or fallback to run.result
  const writerTask = run.pipeline?.find((t) => t.name === 'writer');
  const latexSource = (writerTask?.outputs?.latex_paper as string) || run.result?.latex_paper || '';
  const lineCount = latexSource ? latexSource.split('\n').length : 0;

  const compilePdf = useCallback(async () => {
    setCompiling(true);
    setCompileError('');
    try {
      const res = await apiPost<CompileResponse>(`/api/runs/${run.run_id}/compile-pdf`, {});
      if (res.ok) {
        setPdfAvailable(true);
      } else {
        setCompileError(res.error || 'Compilation failed');
      }
    } catch (e) {
      setCompileError(String(e));
    } finally {
      setCompiling(false);
    }
  }, [run.run_id]);

  return (
    <div className="paper-viewer" style={{ position: 'relative' }}>
      <div className="pv-tab-bar">
        <button
          className={`pv-tab${activeTab === 'pdf' ? ' is-active' : ''}`}
          onClick={() => setActiveTab('pdf')}
        >
          PDF
        </button>
        <button
          className={`pv-tab${activeTab === 'latex' ? ' is-active' : ''}`}
          onClick={() => setActiveTab('latex')}
        >
          LaTeX
        </button>
        <div className="pv-tab-actions">
          <a
            href={`/api/runs/${run.run_id}/artifacts/paper.tex`}
            target="_blank"
            rel="noreferrer"
            className="pv-download-btn"
          >
            ⬇ .tex
          </a>
          <a
            href={pdfUrl}
            target="_blank"
            rel="noreferrer"
            className="pv-download-btn"
          >
            ⬇ .pdf
          </a>
        </div>
      </div>

      <div className="pv-content">
        {activeTab === 'pdf' ? (
          pdfAvailable ? (
            <iframe
              className="pv-pdf-frame"
              src={pdfUrl}
              title="Paper PDF"
            />
          ) : (
            <div className="paper-empty-state">
              {compiling ? (
                <>
                  <div className="paper-progress-dots"><span /><span /><span /></div>
                  <p>Compiling PDF...</p>
                </>
              ) : compileError ? (
                <>
                  <p style={{ color: 'var(--warn)', marginBottom: '1rem' }}>{compileError}</p>
                  <button className="btn btn-primary" onClick={compilePdf}>
                    Retry
                  </button>
                </>
              ) : (
                <>
                  <div className="paper-progress-dots"><span /><span /><span /></div>
                  <p>Preparing PDF...</p>
                </>
              )}
            </div>
          )
        ) : (
          <pre className="pv-latex-source">{latexSource || 'No LaTeX source available.'}</pre>
        )}
      </div>

      <div className="pv-version-bar">
        <span className="pv-version-label">Paper v{paperVersion}</span>
        <span className="pv-version-sep">·</span>
        <span className="pv-version-label">{lineCount} lines</span>
        {isRewriting && (
          <>
            <span className="pv-version-sep">→</span>
            <span className="pv-version-label" style={{ color: 'var(--primary)', fontWeight: 500 }}>
              v{paperVersion + 1} generating...
            </span>
          </>
        )}
        {!isRewriting && pdfAvailable && <span className="pv-compiled-badge">Compiled</span>}
      </div>

      {isRewriting && (
        <RewriteOverlay theoryStatus={theoryStatus} writerStatus={writerStatus} />
      )}
    </div>
  );
}
