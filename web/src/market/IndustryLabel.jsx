import './industry.css'

export default function IndustryLabel({ code, industries }) {
  const industry = industries?.[code]
  return <span className={`stock-industry${industry ? '' : ' missing'}`} title={industry ? `当前申万二级行业：${industry}` : '当前二级行业资料待补充'}>{industry || '行业待补'}</span>
}
