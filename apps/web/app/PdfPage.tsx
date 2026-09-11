"use client";
import { useEffect, useRef, useState } from "react";
export default function PdfPage({
  sourceId,
  page,
  bbox,
}: {
  sourceId: string;
  page: number;
  bbox?: number[] | null;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    let destroy: (() => void) | undefined;
    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
        pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
        const response = await fetch(`/api/backend/sources/${sourceId}/raw`);
        if (!response.ok) throw Error("PDF unavailable");
        const task = pdfjs.getDocument({ data: await response.arrayBuffer() });
        destroy = () => {
          void task.destroy();
        };
        const pdf = await task.promise;
        const p = await pdf.getPage(page);
        if (!active || !ref.current) return;
        const viewport = p.getViewport({ scale: 1.2 });
        const canvas = ref.current;
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        const ctx = canvas.getContext("2d")!;
        await p.render({ canvas, canvasContext: ctx, viewport }).promise;
        if (bbox && active) {
          ctx.strokeStyle = "#edb445";
          ctx.lineWidth = 3;
          ctx.strokeRect(
            bbox[0] * 1.2,
            bbox[1] * 1.2,
            (bbox[2] - bbox[0]) * 1.2,
            (bbox[3] - bbox[1]) * 1.2,
          );
        }
      } catch (e) {
        if (active) setError(String(e));
      }
    })();
    return () => {
      active = false;
      destroy?.();
    };
  }, [sourceId, page, bbox]);
  return (
    <div className="pdf-preview">
      {error ? (
        <p className="error">PDF 预览失败：{error}</p>
      ) : (
        <canvas ref={ref} />
      )}
      <small>第 {page} 页 · 框选为解析定位范围，精确引文见下方文本</small>
    </div>
  );
}
