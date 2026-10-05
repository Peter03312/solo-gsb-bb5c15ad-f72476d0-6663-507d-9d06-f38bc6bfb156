"""请求体模型：负容量等非法输入在此被拦截并返回带定位的 422 错误。"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="水源名称")
    node: str = Field(min_length=1, max_length=64, description="水源所在管网节点")
    max_supply: float = Field(ge=0, description="供水上限，不得为负")


class IncompatiblePairCreate(BaseModel):
    source_a: int = Field(ge=1, description="水源A编号")
    source_b: int = Field(ge=1, description="水源B编号")

    @model_validator(mode="after")
    def _distinct(self):
        if self.source_a == self.source_b:
            raise ValueError("source_a 与 source_b 不能是同一水源")
        return self


class PipeCreate(BaseModel):
    from_node: str = Field(min_length=1, max_length=64, description="起点节点（单向）")
    to_node: str = Field(min_length=1, max_length=64, description="终点节点（单向）")
    capacity: float = Field(ge=0, description="管段容量，负容量将被拒绝")
    valve_open: bool = Field(default=True, description="随管段自动创建的阀门初始状态")


class DemandCreate(BaseModel):
    node: str = Field(min_length=1, max_length=64, description="用水点所在节点")
    demand: float = Field(ge=0, description="需求流量，不得为负")
    priority: int = Field(default=0, description="优先级，数值越大越优先")
    name: str = Field(default="", max_length=64, description="用水点名称")


class ValveChange(BaseModel):
    valve_id: int = Field(ge=1, description="阀门编号")
    open: bool = Field(description="目标状态：true 开启 / false 关闭")


class ValveChangeBatch(BaseModel):
    """阀门变更批次：整批校验、原子生效。"""

    changes: list[ValveChange] = Field(min_length=1)


class RehearsalRequest(BaseModel):
    """隔离预演：给定拟关闭阀门，只演算不写库。"""

    close_valves: list[int] = Field(default_factory=list, description="拟关闭的阀门编号列表")
