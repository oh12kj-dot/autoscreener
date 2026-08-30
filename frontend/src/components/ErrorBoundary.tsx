import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * ページ単位のクラッシュ防止(2026-08-26)。
 *
 * このアプリにはエラーバウンダリが1つも無く、どこか1箇所で未捕捉の例外が
 * 起きると React ツリー全体がアンマウントされ、**画面が何の表示も無いまま
 * 真っ暗になる**——実際に、APIプロセスの再起動忘れで新フィールド
 * (`warnings` 等)が未定義のまま返ってきたとき、`undefined.length` の
 * アクセスがこれを引き起こした(README「トラブルシューティング」参照)。
 *
 * `Layout` の `<Outlet />` をこれで包み、ヘッダー・ナビゲーションは
 * クラッシュの影響を受けないようにする。他の画面へは移動できる状態を保つ。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[ErrorBoundary] uncaught render error:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="error-boundary">
          <strong>この画面の表示中にエラーが発生しました。</strong>
          <p>
            APIのレスポンス形式が古い(プロセスの再起動忘れ)か、コードに不具合がある
            可能性があります。上のナビゲーションから他の画面へ移動できます。
          </p>
          <p className="error-boundary-detail">{this.state.error.message}</p>
        </div>
      );
    }
    return this.props.children;
  }
}
