package execrelaypb

import "github.com/golang/protobuf/proto"

type SignalParam struct {
	Key   string `protobuf:"bytes,1,opt,name=key,proto3" json:"key,omitempty"`
	Value string `protobuf:"bytes,2,opt,name=value,proto3" json:"value,omitempty"`
}

func (m *SignalParam) Reset()         { *m = SignalParam{} }
func (m *SignalParam) String() string { return proto.CompactTextString(m) }
func (*SignalParam) ProtoMessage()    {}

type Signal struct {
	TraceId          string         `protobuf:"bytes,1,opt,name=trace_id,json=traceId,proto3" json:"trace_id,omitempty"`
	LicenseId        string         `protobuf:"bytes,2,opt,name=license_id,json=licenseId,proto3" json:"license_id,omitempty"`
	InstanceId       string         `protobuf:"bytes,3,opt,name=instance_id,json=instanceId,proto3" json:"instance_id,omitempty"`
	Command          string         `protobuf:"bytes,4,opt,name=command,proto3" json:"command,omitempty"`
	RawCommand       string         `protobuf:"bytes,5,opt,name=raw_command,json=rawCommand,proto3" json:"raw_command,omitempty"`
	Symbol           string         `protobuf:"bytes,6,opt,name=symbol,proto3" json:"symbol,omitempty"`
	IngressRegion    string         `protobuf:"bytes,7,opt,name=ingress_region,json=ingressRegion,proto3" json:"ingress_region,omitempty"`
	ReceivedUnixNano int64          `protobuf:"varint,8,opt,name=received_unix_nano,json=receivedUnixNano,proto3" json:"received_unix_nano,omitempty"`
	BodySha256       string         `protobuf:"bytes,9,opt,name=body_sha256,json=bodySha256,proto3" json:"body_sha256,omitempty"`
	Params           []*SignalParam `protobuf:"bytes,10,rep,name=params,proto3" json:"params,omitempty"`
}

func (m *Signal) Reset()         { *m = Signal{} }
func (m *Signal) String() string { return proto.CompactTextString(m) }
func (*Signal) ProtoMessage()    {}
