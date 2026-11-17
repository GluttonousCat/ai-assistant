import { Component } from 'react'

/** Keep a failed feature or chunk load from taking down platform navigation. */
export default class FeatureBoundary extends Component {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (this.state.failed) return <div className="loading" role="alert">
      <p>页面暂时无法加载，请刷新后重试。</p>
      <button className="btn ghost" onClick={() => window.location.reload()}>刷新页面</button>
    </div>
    return this.props.children
  }
}
