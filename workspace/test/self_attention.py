"""
自注意力（Self-Attention）算法实现 —— 仅依赖 NumPy。

核心公式（Scaled Dot-Product Attention）：

    Attention(Q, K, V) = softmax(Q Kᵀ / sqrt(d_k) + M) V

其中 Q、K、V 由同一个输入序列 X 经三组线性投影得到：

    Q = X W_q,  K = X W_k,  V = X W_v

M 是可选掩码（mask），True 表示该位置可见、False 表示被屏蔽；
因果掩码用于让位置 i 只能看到 j <= i 的历史位置。

本文件内容：
    softmax(x, axis)                          数值稳定的 softmax
    causal_mask(seq_len)                      因果（下三角）掩码
    scaled_dot_product_attention(Q, K, V, ..) 注意力核心函数
    SelfAttention                             单头自注意力层
    MultiHeadSelfAttention                    多头自注意力层

用法：
    from self_attention import MultiHeadSelfAttention
    layer = MultiHeadSelfAttention(d_model=64, n_heads=8, seed=0)
    y, attn = layer.forward(x)            # x: (batch, seq_len, d_model)

运行 `python test/self_attention.py` 会执行文件末尾的内置自检。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "softmax",
    "causal_mask",
    "scaled_dot_product_attention",
    "SelfAttention",
    "MultiHeadSelfAttention",
]


def softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    """数值稳定的 softmax：先减去每行最大值，防止 exp 溢出。

    参数
    ----
    x    : 任意形状的数组
    axis : 归一化所在的维度，默认最后一维

    返回
    ----
    与 x 同形状的数组，沿 axis 的每个切片非负且和为 1。
    """
    x = np.asarray(x, dtype=np.float64)
    x = x - np.max(x, axis=axis, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=axis, keepdims=True)


def causal_mask(seq_len: int) -> np.ndarray:
    """生成因果掩码 (seq_len, seq_len)：位置 i 只允许关注 j <= i。

    即下三角（含对角线）为 True，其余为 False。
    """
    if seq_len <= 0:
        raise ValueError("seq_len 必须为正整数")
    return np.tril(np.ones((seq_len, seq_len), dtype=bool))


def scaled_dot_product_attention(
    Q: np.ndarray,
    K: np.ndarray,
    V: np.ndarray,
    mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """缩放点积注意力。

    参数
    ----
    Q    : (..., T_q, d_k)  查询
    K    : (..., T_k, d_k)  键
    V    : (..., T_k, d_v)  值
    mask : 可广播到 (..., T_q, T_k) 的布尔数组；
           True 表示该位置保留，False 表示屏蔽（权重置 0）。
           传 None 表示不做掩码（全可见）。

    返回
    ----
    (context, attn_weights)
        context      : (..., T_q, d_v) 加权求和后的输出
        attn_weights : (..., T_q, T_k) 注意力权重，每行和为 1
                       （若某行被完全屏蔽，则该行全为 0）
    """
    Q = np.asarray(Q, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    V = np.asarray(V, dtype=np.float64)

    if Q.ndim < 2 or K.ndim < 2 or V.ndim < 2:
        raise ValueError("Q/K/V 至少需要 2 个维度 (..., T, d)")
    if Q.shape[-1] != K.shape[-1]:
        raise ValueError(f"Q 与 K 的最后一维（d_k）必须相同：{Q.shape[-1]} vs {K.shape[-1]}")
    if K.shape[-2] != V.shape[-2]:
        raise ValueError(f"K 与 V 的序列长度必须相同：{K.shape[-2]} vs {V.shape[-2]}")

    d_k = Q.shape[-1]

    # 1) 相似度打分：Q Kᵀ / sqrt(d_k)
    scores = (Q @ np.swapaxes(K, -1, -2)) / math.sqrt(d_k)

    fully_masked = None
    if mask is not None:
        mask = np.broadcast_to(np.asarray(mask, dtype=bool), scores.shape)
        fully_masked = ~np.any(mask, axis=-1, keepdims=True)
        scores = np.where(mask, scores, -np.inf)
        # 整行都被屏蔽时先填 0，避免 softmax 出现 nan（结果随后再清零）
        scores = np.where(fully_masked, 0.0, scores)

    # 2) 按最后一维做 softmax 得到注意力权重
    attn_weights = softmax(scores, axis=-1)
    if mask is not None:
        attn_weights = np.where(fully_masked, 0.0, attn_weights)

    # 3) 用权重对 V 做加权求和
    context = attn_weights @ V
    return context, attn_weights


class SelfAttention:
    """单头自注意力层。

    对输入 x: (..., T, d_model) 做三组线性投影得到 Q/K/V，
    再调用 scaled_dot_product_attention 得到输出。

    参数
    ----
    d_model : 输入特征维度
    d_k     : Q/K 的维度，默认等于 d_model
    d_v     : V 的维度，默认等于 d_model
    seed    : 随机种子；None 表示每次运行随机初始化
    """

    def __init__(
        self,
        d_model: int,
        d_k: int | None = None,
        d_v: int | None = None,
        seed: int | None = 0,
    ) -> None:
        if d_model <= 0:
            raise ValueError("d_model 必须为正整数")
        self.d_model = int(d_model)
        self.d_k = int(d_k) if d_k is not None else self.d_model
        self.d_v = int(d_v) if d_v is not None else self.d_model

        rng = np.random.default_rng(seed)
        # Glorot 均匀初始化：limit = sqrt(6 / (fan_in + fan_out))
        self.W_q = self._init((self.d_model, self.d_k), rng)
        self.W_k = self._init((self.d_model, self.d_k), rng)
        self.W_v = self._init((self.d_model, self.d_v), rng)

    @staticmethod
    def _init(shape: tuple[int, int], rng: np.random.Generator) -> np.ndarray:
        fan_in, fan_out = shape
        limit = math.sqrt(6.0 / (fan_in + fan_out))
        return rng.uniform(-limit, limit, size=shape)

    def project(
        self, x: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """线性投影得到 Q、K、V。"""
        x = np.asarray(x, dtype=np.float64)
        if x.ndim < 2 or x.shape[-1] != self.d_model:
            raise ValueError(f"输入最后一维必须是 d_model={self.d_model}，实际为 {x.shape}")
        return x @ self.W_q, x @ self.W_k, x @ self.W_v

    def forward(
        self, x: np.ndarray, mask: np.ndarray | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """前向计算，返回 (输出, 注意力权重)。"""
        Q, K, V = self.project(x)
        return scaled_dot_product_attention(Q, K, V, mask)

    __call__ = forward


class MultiHeadSelfAttention:
    """多头自注意力层。

    把 d_model 维度切成 n_heads 份并行做注意力，再拼接后做一次输出投影 W_o。

    参数
    ----
    d_model : 输入/输出特征维度，必须能被 n_heads 整除
    n_heads : 头数
    seed    : 随机种子
    """

    def __init__(self, d_model: int, n_heads: int = 8, seed: int | None = 0) -> None:
        if d_model <= 0 or n_heads <= 0:
            raise ValueError("d_model 与 n_heads 必须为正整数")
        if d_model % n_heads != 0:
            raise ValueError(f"d_model({d_model}) 必须能被 n_heads({n_heads}) 整除")
        self.d_model = int(d_model)
        self.n_heads = int(n_heads)
        self.d_k = self.d_model // self.n_heads

        rng = np.random.default_rng(seed)
        self.W_q = SelfAttention._init((self.d_model, self.d_model), rng)
        self.W_k = SelfAttention._init((self.d_model, self.d_model), rng)
        self.W_v = SelfAttention._init((self.d_model, self.d_model), rng)
        self.W_o = SelfAttention._init((self.d_model, self.d_model), rng)

    def _split_heads(self, t: np.ndarray) -> np.ndarray:
        """(..., T, d_model) -> (..., n_heads, T, d_k)"""
        *lead, T, _ = t.shape
        return t.reshape(*lead, T, self.n_heads, self.d_k).swapaxes(-2, -3)

    def _merge_heads(self, t: np.ndarray) -> np.ndarray:
        """(..., n_heads, T, d_k) -> (..., T, d_model)"""
        *lead, H, T, d_k = t.shape
        return t.swapaxes(-2, -3).reshape(*lead, T, H * d_k)

    def forward(
        self, x: np.ndarray, mask: np.ndarray | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """前向计算，返回 (输出, 注意力权重)。

        输出        : (..., T, d_model)
        注意力权重  : (..., n_heads, T, T)（mask 会广播到每个头）
        """
        x = np.asarray(x, dtype=np.float64)
        if x.ndim < 2 or x.shape[-1] != self.d_model:
            raise ValueError(f"输入最后一维必须是 d_model={self.d_model}，实际为 {x.shape}")

        Q = self._split_heads(x @ self.W_q)
        K = self._split_heads(x @ self.W_k)
        V = self._split_heads(x @ self.W_v)

        context, attn_weights = scaled_dot_product_attention(Q, K, V, mask)
        out = self._merge_heads(context) @ self.W_o
        return out, attn_weights

    __call__ = forward


# --------------------------------------------------------------------------- #
# 内置自检：直接运行本文件时执行
# --------------------------------------------------------------------------- #
def _naive_attention_2d(Q, K, V, mask=None):
    """用显式双层循环实现的参考版本，用来校验向量化实现。"""
    T_q, d_k = Q.shape
    T_k = K.shape[0]
    scores = np.empty((T_q, T_k))
    for i in range(T_q):
        for j in range(T_k):
            scores[i, j] = float(np.dot(Q[i], K[j])) / math.sqrt(d_k)
    if mask is not None:
        scores = np.where(mask, scores, -np.inf)
    attn = np.empty((T_q, T_k))
    for i in range(T_q):
        e = np.exp(scores[i] - np.max(scores[i]))
        attn[i] = e / e.sum()
    ctx = np.empty((T_q, V.shape[1]))
    for i in range(T_q):
        ctx[i] = sum(attn[i, j] * V[j] for j in range(T_k))
    return ctx, attn


def _selftest() -> None:
    rng = np.random.default_rng(123)
    checks: list[tuple[str, bool]] = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # 1) softmax 沿指定维度归一化
    s = softmax(rng.normal(size=(4, 7)))
    check("softmax 每行和为 1", np.allclose(s.sum(axis=-1), 1.0))
    check("softmax 非负", np.all(s >= 0))
    check("softmax 数值稳定(大输入不产生 nan)", not np.isnan(softmax(np.array([[1e4, 1e4, -1e4]]))).any())

    # 2) 向量化实现与显式循环参考实现一致（含 sqrt(d_k) 缩放和 mask）
    T, d_k, d_v = 5, 8, 6
    Q, K, V = (rng.normal(size=(T, d_k)), rng.normal(size=(T, d_k)), rng.normal(size=(T, d_v)))
    ctx_ref, attn_ref = _naive_attention_2d(Q, K, V)
    ctx, attn = scaled_dot_product_attention(Q, K, V)
    check("与朴素实现对拍(context)", np.allclose(ctx, ctx_ref))
    check("与朴素实现对拍(attn)", np.allclose(attn, attn_ref))

    # 3) 因果掩码：未来位置权重必须为 0，且每行仍和为 1
    m = causal_mask(T)
    ctx_c, attn_c = scaled_dot_product_attention(Q, K, V, m)
    check("因果掩码上三角为 0", np.allclose(attn_c[np.triu_indices(T, k=1)], 0.0))
    check("因果掩码每行和为 1", np.allclose(attn_c.sum(axis=-1), 1.0))
    check("因果掩码下三角非零", np.all(attn_c[~np.triu(np.ones((T, T), dtype=bool), k=1)] > 0))
    ctx_ref_c, _ = _naive_attention_2d(Q, K, V, m)
    check("因果掩码与朴素实现对拍", np.allclose(ctx_c, ctx_ref_c))

    # 4) 整行被屏蔽时输出为 0 且不产生 nan
    zero = np.zeros((T, T), dtype=bool)
    ctx_z, attn_z = scaled_dot_product_attention(Q, K, V, zero)
    check("全屏蔽不产生 nan", not (np.isnan(ctx_z).any() or np.isnan(attn_z).any()))
    check("全屏蔽输出为 0", np.allclose(ctx_z, 0.0))

    # 5) 单头层：形状与权重归一化
    sa = SelfAttention(d_model=16, d_k=8, d_v=12, seed=1)
    x = rng.normal(size=(3, 7, 16))
    y, w = sa.forward(x)
    check("单头输出形状", y.shape == (3, 7, 12))
    check("单头权重形状", w.shape == (3, 7, 7))
    check("单头权重每行和为 1", np.allclose(w.sum(axis=-1), 1.0))
    check("单头支持 batch=1/2 维输入", sa.forward(x[0]).shape == (7, 12))

    # 6) 多头层：形状、头数、权重行和为 1
    mh = MultiHeadSelfAttention(d_model=16, n_heads=4, seed=2)
    xb = rng.normal(size=(2, 6, 16))
    yb, wb = mh.forward(xb)
    check("多头输出形状", yb.shape == (2, 6, 16))
    check("多头权重形状", wb.shape == (2, 4, 6, 6))
    check("多头权重每行和为 1", np.allclose(wb.sum(axis=-1), 1.0))
    yb_c, wb_c = mh.forward(xb, causal_mask(6))
    check("多头因果掩码上三角为 0", np.allclose(wb_c[..., np.triu_indices(6, k=1)[0], np.triu_indices(6, k=1)[1]], 0.0))

    # 7) 单头(1 头)多头层 + 恒等 W_o 应等价于同参数的单头层
    mh1 = MultiHeadSelfAttention(d_model=8, n_heads=1, seed=3)
    sa1 = SelfAttention(d_model=8, seed=3)
    mh1.W_q, mh1.W_k, mh1.W_v = sa1.W_q.copy(), sa1.W_k.copy(), sa1.W_v.copy()
    mh1.W_o = np.eye(8)
    x1 = rng.normal(size=(2, 5, 8))
    check("1 头多头层等价于单头层", np.allclose(mh1(x1)[0], sa1(x1)[0]))

    # 8) 置换等变性：打乱 token 顺序，输出随之打乱
    perm = rng.permutation(7)
    y_perm, _ = sa.forward(x[:, perm, :])
    check("置换等变性", np.allclose(y_perm, y[:, perm, :]))

    # 9) 无 mask 时，输出恰好是 V 的凸组合（权重非负、和为 1）
    Qs, Ks, Vs = sa.project(x)
    check("注意力输出位于 V 的凸包内", np.all(ctx.min() >= Vs.min() - 1e-9) and np.all(ctx.max() <= Vs.max() + 1e-9))

    # 10) 异常输入应有清晰报错
    def raises(fn):
        try:
            fn()
        except (ValueError, TypeError):
            return True
        return False

    check("维度不匹配时报错", raises(lambda: scaled_dot_product_attention(Q, K[:, :4], V)))
    check("d_model 不整除时报错", raises(lambda: MultiHeadSelfAttention(10, 4)))

    passed = sum(ok for _, ok in checks)
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(checks)} 项自检通过")
    if passed != len(checks):
        raise SystemExit(1)

    # 打印一个小例子，方便直观感受注意力权重分布
    print("\n示例：seq_len=4, d_model=8, 1 个头的注意力权重")
    demo = MultiHeadSelfAttention(d_model=8, n_heads=1, seed=42)
    _, demo_w = demo(rng.normal(size=(4, 8)))
    print(np.round(demo_w, 3))


if __name__ == "__main__":
    _selftest()
